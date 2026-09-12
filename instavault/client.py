"""Instagram access layer.

Wraps Instaloader so nothing else in the app touches it directly, and turns its
exceptions into something a user interface can act on: two-factor prompts,
checkpoint challenges, expired sessions and rate limits are all distinct states
rather than one generic failure.
"""

from __future__ import annotations

import random
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import instaloader
from instaloader import exceptions as ie

from . import config, events


class AuthError(Exception):
    """Login failed in a way the user has to resolve."""

    def __init__(self, message: str, code: str = "error", hint: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {"error": self.message, "code": self.code, "hint": self.hint}


class Cancelled(Exception):
    """Raised when a running job was asked to stop."""


# --------------------------------------------------------------------- rate limit


class RateLimiter:
    """Enforces a floor between outbound requests, with adaptive backoff.

    Instaloader jitters its own requests, but only per request type. This is a
    global floor so a burst of downloads cannot outrun it, and it widens
    automatically the moment Instagram pushes back with a 429.
    """

    def __init__(self, base_delay: float):
        self.base_delay = base_delay
        self.penalty = 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    @property
    def delay(self) -> float:
        return self.base_delay + self.penalty

    def wait(self, stop: threading.Event | None = None) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            remaining = self.delay - gap
            self._last = time.monotonic() + max(remaining, 0)

        if remaining > 0:
            jitter = random.uniform(0, 0.4 * self.base_delay)
            if stop is not None:
                if stop.wait(remaining + jitter):
                    raise Cancelled()
            else:
                time.sleep(remaining + jitter)

    def penalise(self) -> float:
        """Called after a rate-limit response. Returns the cooldown to observe."""
        self.penalty = min(self.penalty * 2 + 2.0, 30.0)
        return config.RATE_LIMIT_BACKOFF

    def relax(self) -> None:
        """Gradually undo the penalty after a run of clean requests."""
        self.penalty = max(0.0, self.penalty - 0.5)


limiter = RateLimiter(config.REQUEST_DELAY)


# ----------------------------------------------------------------- error mapping


def _exc(name: str) -> type:
    """Look an Instaloader exception up by name, tolerating version drift."""
    return getattr(ie, name, type("_Missing", (Exception,), {}))


RATE_LIMIT_ERRORS = (_exc("TooManyRequestsException"),)
LOGIN_ERRORS = (_exc("LoginRequiredException"), _exc("LoginException"))


def is_rate_limited(exc: Exception) -> bool:
    if isinstance(exc, RATE_LIMIT_ERRORS):
        return True
    text = str(exc).lower()
    return "429" in text or "please wait a few minutes" in text


def is_session_expired(exc: Exception) -> bool:
    if isinstance(exc, _exc("LoginRequiredException")):
        return True
    text = str(exc).lower()
    return "login_required" in text or "not logged in" in text


def describe(exc: Exception) -> AuthError:
    """Translate an Instaloader exception into something worth showing a user."""
    text = str(exc)
    low = text.lower()

    if isinstance(exc, _exc("BadCredentialsException")):
        return AuthError("Wrong username or password.", "bad_credentials")

    if isinstance(exc, _exc("TwoFactorAuthRequiredException")):
        return AuthError("Two-factor code required.", "two_factor")

    if "checkpoint" in low or "challenge" in low:
        return AuthError(
            "Instagram wants you to verify this login.",
            "challenge",
            "Open Instagram on your phone, approve the login prompt, then try again. "
            "If a link was shown above, opening it in a browser also clears the check.",
        )

    if is_rate_limited(exc):
        return AuthError(
            "Instagram is rate limiting this account.",
            "rate_limited",
            "Wait 10-15 minutes before trying again, and raise REQUEST_DELAY in .env.",
        )

    if is_session_expired(exc):
        return AuthError("Your saved session expired.", "session_expired",
                         "Sign in again to refresh it.")

    if isinstance(exc, _exc("ConnectionException")):
        return AuthError("Could not reach Instagram.", "connection",
                         "Check your internet connection and try again.")

    return AuthError(text or exc.__class__.__name__, "error")


# ----------------------------------------------------------------------- session


@dataclass
class Session:
    """The app's authentication state. One per process."""

    username: str = ""
    loader: instaloader.Instaloader | None = None
    pending_2fa: instaloader.Instaloader | None = None
    pending_username: str = ""
    last_error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def authenticated(self) -> bool:
        return self.loader is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "authenticated": self.authenticated,
            "awaiting_two_factor": self.pending_2fa is not None,
            "last_error": self.last_error,
        }


session = Session()


def _session_file(username: str) -> Path:
    return config.SESSION_DIR / f"{username}.session"


def build_loader() -> instaloader.Instaloader:
    config.ensure_dirs()
    return instaloader.Instaloader(
        dirname_pattern=str(config.DOWNLOAD_DIR / "{target}"),
        filename_pattern="{shortcode}_{date_utc}",
        download_comments=False,
        download_geotags=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        quiet=True,
        max_connection_attempts=2,
    )


def restore(username: str) -> bool:
    """Load a cached session from disk. Returns False when there isn't one."""
    path = _session_file(username)
    if not path.exists():
        return False

    loader = build_loader()
    try:
        loader.load_session_from_file(username, str(path))
        # load_session_from_file does not verify, so make one cheap authenticated
        # call to find out whether the cookie is actually still good.
        if loader.test_login() != username:
            return False
    except Exception as exc:
        events.log(f"Cached session for {username} is unusable: {exc}", "warn")
        return False

    with session.lock:
        session.loader = loader
        session.username = username
        session.last_error = ""
    events.publish("auth", **session.as_dict())
    return True


def login(username: str, password: str) -> dict[str, Any]:
    """Password login. May end in a two-factor prompt rather than a session."""
    loader = build_loader()
    try:
        loader.login(username, password)
    except _exc("TwoFactorAuthRequiredException"):
        with session.lock:
            session.pending_2fa = loader
            session.pending_username = username
        events.publish("auth", **session.as_dict())
        return {"two_factor_required": True, "username": username}
    except Exception as exc:
        err = describe(exc)
        with session.lock:
            session.last_error = err.message
        raise err from exc

    _finish_login(loader, username)
    return {"authenticated": True, "username": username}


def two_factor(code: str) -> dict[str, Any]:
    with session.lock:
        loader = session.pending_2fa
        username = session.pending_username

    if loader is None:
        raise AuthError("No two-factor login in progress. Start again.", "no_pending")

    try:
        loader.two_factor_login(code)
    except Exception as exc:
        err = describe(exc)
        if err.code == "error":
            err = AuthError("That code was not accepted. Try the next one.", "bad_code")
        raise err from exc

    with session.lock:
        session.pending_2fa = None
        session.pending_username = ""

    _finish_login(loader, username)
    return {"authenticated": True, "username": username}


def _finish_login(loader: instaloader.Instaloader, username: str) -> None:
    config.ensure_dirs()
    try:
        loader.save_session_to_file(str(_session_file(username)))
    except Exception as exc:
        events.log(f"Could not cache the session: {exc}", "warn")

    with session.lock:
        session.loader = loader
        session.username = username
        session.last_error = ""

    events.publish("auth", **session.as_dict())
    events.log(f"Signed in as {username}", "success")


def logout(forget: bool = False) -> None:
    with session.lock:
        username = session.username
        session.loader = None
        session.pending_2fa = None
        session.username = "" if forget else username

    if forget and username:
        _session_file(username).unlink(missing_ok=True)

    events.publish("auth", **session.as_dict())


def require_loader() -> instaloader.Instaloader:
    with session.lock:
        if session.loader is None:
            raise AuthError("Not signed in.", "unauthenticated")
        return session.loader


def handle_auth_loss(exc: Exception) -> None:
    """If an error means the session died, drop it so the UI can prompt again."""
    if is_session_expired(exc):
        events.log("Instagram invalidated the session - sign in again.", "error")
        logout()


# ------------------------------------------------------------------- saved feed


def _safe(getter: Callable[[], Any], default: Any = None) -> Any:
    """Post attributes can trigger extra requests or be missing; never crash."""
    try:
        value = getter()
    except Exception:
        return default
    return default if value is None else value


def _to_row(post: Any, collection: str = "") -> dict[str, Any]:
    taken = _safe(lambda: post.date_utc)
    return {
        "shortcode": post.shortcode,
        "typename": _safe(lambda: post.typename, "GraphImage"),
        "is_video": 1 if _safe(lambda: post.is_video, False) else 0,
        "caption": (_safe(lambda: post.caption, "") or "")[:500],
        "owner": _safe(lambda: post.owner_username, "") or "",
        "thumb_url": _safe(lambda: post.url, "") or "",
        "taken_at": (taken or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "media_count": int(_safe(lambda: post.mediacount, 1) or 1),
        "video_duration": _safe(lambda: post.video_duration),
        "collection": collection,
        "discovered_at": None,
    }


def iter_saved(
    stop: threading.Event | None = None,
    limit: int | None = None,
    on_page: Callable[[list[dict[str, Any]]], None] | None = None,
    page_size: int = 24,
) -> Iterator[dict[str, Any]]:
    """Walk the Saved feed, yielding rows and flushing them in batches.

    Batching matters: the caller can persist and show results as they arrive
    instead of waiting for a full walk of an account with thousands of saves.
    """
    loader = require_loader()
    profile = instaloader.Profile.own_profile(loader.context)

    batch: list[dict[str, Any]] = []
    count = 0

    try:
        for post in profile.get_saved_posts():
            if stop is not None and stop.is_set():
                raise Cancelled()

            row = _to_row(post)
            batch.append(row)
            yield row
            count += 1

            if len(batch) >= page_size:
                if on_page:
                    on_page(batch)
                batch = []
                limiter.relax()

            if limit is not None and count >= limit:
                break
    except Cancelled:
        raise
    except Exception as exc:
        handle_auth_loss(exc)
        if batch and on_page:
            on_page(batch)
        raise
    finally:
        if batch and on_page:
            on_page(batch)


def fetch_collections() -> list[dict[str, Any]]:
    """Best-effort read of named collections via Instagram's private endpoint.

    There is no supported API for this and the shape changes without notice, so
    a failure here is logged and swallowed - the app falls back to one flat
    Saved feed.
    """
    loader = require_loader()
    try:
        limiter.wait()
        data = loader.context.get_json(
            "api/v1/collections/list/",
            params={"collection_types": '["MEDIA"]'},
            host="www.instagram.com",
        )
    except Exception as exc:
        events.log(f"Collections unavailable ({describe(exc).message}) - "
                   f"showing one flat Saved feed.", "warn")
        return []

    found = []
    for entry in (data or {}).get("items", []):
        info = entry.get("collection_id") and entry or entry.get("collection", {})
        name = info.get("collection_name") or entry.get("collection_name")
        if not name:
            continue
        found.append(
            {
                "id": str(info.get("collection_id") or entry.get("collection_id") or ""),
                "name": name,
                "count": int(entry.get("collection_media_count") or 0),
            }
        )
    return found


# --------------------------------------------------------------------- downloads


def _dir_stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def _extract_audio(item_dir: Path) -> int:
    """Split the audio track out of any downloaded video. Needs ffmpeg."""
    ffmpeg = config.ffmpeg_path()
    if not ffmpeg:
        return 0

    made = 0
    for video in list(item_dir.glob("*.mp4")):
        target = video.with_suffix(".m4a")
        if target.exists():
            continue
        for args in (
            [ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "copy", str(target)],
            [ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "aac", str(target)],
        ):
            try:
                result = subprocess.run(args, capture_output=True, timeout=120)
                if result.returncode == 0 and target.exists():
                    made += 1
                    break
            except Exception:
                continue
    return made


def download_item(
    shortcode: str,
    folder: str = "saved",
    stop: threading.Event | None = None,
    extract_audio: bool | None = None,
) -> dict[str, Any]:
    """Download one item into its own folder, retrying transient failures."""
    loader = require_loader()
    safe_folder = "".join(c for c in folder if c.isalnum() or c in " -_") or "saved"
    item_dir = config.DOWNLOAD_DIR / safe_folder / shortcode
    attempts = max(1, config.MAX_RETRIES)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        if stop is not None and stop.is_set():
            raise Cancelled()

        try:
            limiter.wait(stop)
            post = instaloader.Post.from_shortcode(loader.context, shortcode)

            item_dir.mkdir(parents=True, exist_ok=True)
            # A literal dirname_pattern puts every item in its own folder, which
            # makes byte accounting and resume checks trivial.
            loader.dirname_pattern = str(item_dir)
            loader.download_post(post, target=shortcode)

            size, files = _dir_stats(item_dir)
            if files == 0:
                raise RuntimeError("Instagram returned no media for this item.")

            audio = 0
            want_audio = config.EXTRACT_AUDIO if extract_audio is None else extract_audio
            if want_audio and post.is_video:
                audio = _extract_audio(item_dir)
                if audio:
                    size, files = _dir_stats(item_dir)

            limiter.relax()
            return {
                "shortcode": shortcode,
                "ok": True,
                "path": str(item_dir),
                "bytes": size,
                "files": files,
                "audio_tracks": audio,
            }

        except Cancelled:
            raise
        except Exception as exc:
            last_error = exc
            handle_auth_loss(exc)

            if isinstance(exc, AuthError) or is_session_expired(exc):
                break

            if is_rate_limited(exc):
                cooldown = limiter.penalise()
                events.log(
                    f"Rate limited - pausing {int(cooldown)}s before retrying "
                    f"{shortcode}.",
                    "warn",
                )
                if stop is not None and stop.wait(cooldown):
                    raise Cancelled()
                elif stop is None:
                    time.sleep(cooldown)
                continue

            if attempt < attempts:
                backoff = min(2 ** attempt, 15)
                events.log(
                    f"{shortcode} failed ({exc}); retry {attempt}/{attempts - 1} "
                    f"in {backoff}s.",
                    "warn",
                )
                if stop is not None and stop.wait(backoff):
                    raise Cancelled()
                elif stop is None:
                    time.sleep(backoff)

    message = describe(last_error).message if last_error else "Unknown error"
    return {"shortcode": shortcode, "ok": False, "error": message}
