"""Instagram access layer.

Wraps Instaloader so nothing else in the app touches it directly, and turns its
exceptions into something a user interface can act on: two-factor prompts,
checkpoint challenges, expired sessions and rate limits are all distinct states
rather than one generic failure.
"""

from __future__ import annotations

import random
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import instaloader
import requests
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
        link = re.search(r"https?://\S+", text)
        hint = (
            "Approve the login on the Instagram app, then retry. If it keeps "
            "failing, use a browser session instead - that skips the password "
            "login Instagram is objecting to."
        )
        if link:
            hint = f"Open {link.group(0)} in your browser to clear it, then retry. " + hint
        return AuthError("Instagram wants you to verify this login.", "challenge", hint)

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


def login_with_session_cookie(sessionid: str, username: str = "", csrftoken: str = "") -> dict[str, Any]:
    """Adopt an existing browser session instead of logging in with a password.

    Instagram frequently blocks password logins from non-browser clients with a
    checkpoint. A session cookie from a browser that is already signed in
    sidesteps that entirely, because the session is one Instagram already
    trusts.
    """
    sessionid = sessionid.strip().strip('"')
    if not sessionid:
        raise AuthError("Paste the sessionid cookie value.", "missing_sessionid")

    cookies = {
        "sessionid": sessionid,
        # load_session requires a csrftoken; for the read-only calls this app
        # makes, any value works as long as cookie and header agree.
        "csrftoken": csrftoken.strip() or secrets.token_hex(16),
    }

    # sessionid starts with the numeric user id, url-encoded as "<id>%3A...".
    user_id = urllib.parse.unquote(sessionid).split(":")[0]
    if user_id.isdigit():
        cookies["ds_user_id"] = user_id

    loader = build_loader()
    try:
        loader.load_session(username or "unknown", cookies)
        resolved = loader.test_login()
    except Exception as exc:
        raise describe(exc) from exc

    if not resolved:
        raise AuthError(
            "That session cookie was rejected.",
            "bad_session",
            "It may have expired, or been copied incompletely. Sign out and back "
            "into Instagram in your browser, then copy the sessionid again.",
        )

    if username and resolved.lower() != username.lower():
        events.log(f"Session belongs to {resolved}, not {username} - using {resolved}.", "warn")

    _finish_login(loader, resolved)
    return {"authenticated": True, "username": resolved}


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


# Instagram media_type codes from the mobile API.
_PHOTO, _VIDEO, _ALBUM = 1, 2, 8


def _best_thumb(media: dict[str, Any]) -> str:
    """Pull a display URL out of a mobile-API media object.

    Carousels carry no top-level image, so fall back to the first child.
    """
    candidates = (media.get("image_versions2") or {}).get("candidates") or []
    if not candidates:
        children = media.get("carousel_media") or []
        if children:
            candidates = (children[0].get("image_versions2") or {}).get("candidates") or []
    return candidates[0]["url"] if candidates else ""


def _audio_meta(media: dict[str, Any]) -> dict[str, Any]:
    """Audio details for a reel, and which route may fetch it.

    Creator-made sound keeps its direct URL. For a licensed catalog track we
    deliberately keep the title and artist but *not* the asset URL - that URL is
    the commercial master, and the audio for those reels is extracted from the
    reel's own video instead. Not storing it keeps the rule enforced at the data
    layer rather than relying on call sites to remember.
    """
    blank = {
        "audio_url": "",
        "audio_title": "",
        "audio_artist": "",
        "audio_kind": "",
        "audio_asset_id": "",
    }

    clips = media.get("clips_metadata") or {}
    if not clips:
        return blank

    original = clips.get("original_sound_info") or {}
    if original:
        return {
            "audio_url": original.get("progressive_download_url") or "",
            "audio_title": (original.get("original_audio_title") or "")[:200],
            "audio_artist": (original.get("ig_artist") or {}).get("username", "") or "",
            "audio_kind": "original",
            "audio_asset_id": str(original.get("audio_asset_id") or ""),
        }

    asset = (clips.get("music_info") or {}).get("music_asset_info") or {}
    if asset:
        return {
            "audio_url": "",  # catalog master - intentionally not stored
            "audio_title": (asset.get("title") or "")[:200],
            "audio_artist": (asset.get("display_artist") or "")[:200],
            "audio_kind": "music",
            "audio_asset_id": str(asset.get("audio_asset_id") or ""),
        }

    return blank


def _row_from_media(media: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one mobile-API media object into an index row."""
    shortcode = media.get("code")
    if not shortcode:
        return None

    media_type = media.get("media_type")
    if media_type == _ALBUM:
        typename, is_video = "GraphSidecar", 0
    elif media_type == _VIDEO:
        typename, is_video = "GraphVideo", 1
    else:
        typename, is_video = "GraphImage", 0

    caption = media.get("caption")
    caption_text = (caption or {}).get("text", "") if isinstance(caption, dict) else ""

    taken = media.get("taken_at")
    taken_iso = (
        datetime.fromtimestamp(taken, timezone.utc)
        if isinstance(taken, (int, float))
        else datetime.now(timezone.utc)
    ).isoformat(timespec="seconds")

    # Instagram won't hand us collection names over a cookie session, but each
    # saved item carries the ids of the custom collections it's filed under. We
    # keep the first so items can be grouped; names are mapped separately.
    collection_ids = media.get("saved_collection_ids") or []
    collection = str(collection_ids[0]) if collection_ids else ""

    return {
        "shortcode": shortcode,
        "typename": typename,
        "is_video": is_video,
        "caption": (caption_text or "")[:500],
        "owner": (media.get("user") or {}).get("username", "") or "",
        "thumb_url": _best_thumb(media),
        "taken_at": taken_iso,
        "media_count": len(media.get("carousel_media") or []) or 1,
        "video_duration": media.get("video_duration"),
        "collection": collection,
        "discovered_at": None,
        **_audio_meta(media),
    }


def iter_saved(
    stop: threading.Event | None = None,
    limit: int | None = None,
    on_page: Callable[[list[dict[str, Any]]], None] | None = None,
    page_size: int = 24,
) -> Iterator[dict[str, Any]]:
    """Walk the Saved feed via Instagram's mobile API, flushing per page.

    The old GraphQL saved-media endpoint instaloader ships is deprecated - it
    redirects to login and looks like an expired session. The mobile endpoint
    ``feed/saved/posts/`` is the one Instagram still serves, and it returns full
    media objects, so there is no per-item lookup to walk into more dead ends.
    """
    loader = require_loader()
    context = loader.context

    count = 0
    max_id: str | None = None

    try:
        while True:
            if stop is not None and stop.is_set():
                raise Cancelled()

            limiter.wait(stop)
            params: dict[str, Any] = {}
            if max_id:
                params["max_id"] = max_id

            data = context.get_iphone_json("api/v1/feed/saved/posts/", params)

            batch: list[dict[str, Any]] = []
            for entry in data.get("items", []):
                media = entry.get("media") or entry
                row = _row_from_media(media)
                if row is None:
                    continue
                batch.append(row)
                yield row
                count += 1
                if limit is not None and count >= limit:
                    break

            if batch and on_page:
                on_page(batch)
            limiter.relax()

            if limit is not None and count >= limit:
                break
            if not data.get("more_available"):
                break
            max_id = data.get("next_max_id")
            if not max_id:
                break
    except Cancelled:
        raise
    except Exception as exc:
        handle_auth_loss(exc)
        raise


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


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov"}

# Right after Instaloader writes a file it still carries its own naming
# pattern ("{shortcode}_{date}"), so this only needs to work at that moment -
# the file is renamed to its final name immediately afterward.
def _item_files(folder_dir: Path, shortcode: str) -> list[Path]:
    if not folder_dir.exists():
        return []
    return sorted(p for p in folder_dir.iterdir() if p.is_file() and shortcode in p.name)


# Characters Windows forbids in a filename, plus control codes.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Pictographs and flags only - every actual script is left intact, so Telugu,
# Devanagari, Japanese and Chinese titles survive unchanged.
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff️‍]+"
)

# Keeps the longest path comfortably inside the Windows 260-character limit.
NAME_LIMIT = 110


def _safe_name(text: Any, limit: int = 60) -> str:
    """Make a filename fragment safe without flattening non-Latin scripts."""
    cleaned = _EMOJI.sub("", str(text or ""))
    cleaned = _ILLEGAL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].strip(" .")


def build_name(meta: dict[str, Any], mode: str = "full") -> str:
    """The base filename.

    Video: just the account. Licensed audio: just the title - real song titles
    are already distinct. Original-sound audio: account + title, because
    Instagram's generic default title ("Original audio") repeats across
    hundreds of accounts and the account name is what actually distinguishes
    them once there's no per-account folder to do that job.

    Collisions still possible within one account (several sounds that are all
    literally "Original audio") are resolved separately by _unique_path, which
    appends " (2)", " (3)" - so this only has to decide the *ideal* name.
    """
    if mode == "audio":
        title = _safe_name(meta.get("audio_title"), NAME_LIMIT) or "Untitled"
        if meta.get("audio_kind") == "original":
            account = _safe_name(meta.get("audio_artist") or meta.get("owner"), 40) or "unknown"
            return f"{account} - {title}"[:NAME_LIMIT].strip(" .-")
        return title
    return _safe_name(meta.get("owner"), NAME_LIMIT) or "unknown"


def audio_destination(kind: str, artist: str = "") -> str:
    """Classification folder for an audio download, relative to downloads/.

    Just the two-way split: audio/Original Sounds/ for creator-made sounds,
    audio/Licensed Music/ for catalog tracks - no per-account or per-artist
    subfolder. `artist` isn't used for the folder any more, but the parameter
    stays so call sites don't need to change if that ever comes back.
    """
    bucket = "Original Sounds" if kind == "original" else "Licensed Music"
    return f"audio/{bucket}"


def _unique_path(directory: Path, base: str, suffix: str) -> Path:
    """A path guaranteed not to already exist, numbering on collision."""
    candidate = directory / f"{base}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base} ({counter}){suffix}"
        counter += 1
    return candidate


def _rename_item_files(paths: list[Path], directory: Path, base: str) -> list[Path]:
    """Rename downloaded files to the readable scheme, keeping extensions."""
    renamed = []
    for path in paths:
        # Instaloader suffixes carousel members _1, _2 - keep that ordering.
        match = re.search(r"_(\d+)$", path.stem)
        index = f" {match.group(1)}" if match else ""
        target = _unique_path(directory, f"{base}{index}", path.suffix)

        if target == path:
            renamed.append(path)
            continue
        try:
            path.rename(target)
            renamed.append(target)
        except OSError:
            renamed.append(path)   # keep the original rather than lose the file
    return renamed


def _file_stats(paths: list[Path]) -> tuple[int, int]:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total, len(paths)


def _strip_images(paths: list[Path]) -> int:
    """Drop poster/thumbnail images; only the media itself is wanted."""
    removed = 0
    for path in paths:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _extract_audio(videos: list[Path]) -> list[Path]:
    """Split the audio track out of downloaded videos. Needs ffmpeg.

    Returns the audio files actually created, so the caller can add them to its
    file list without re-scanning the folder.
    """
    ffmpeg = config.ffmpeg_path()
    if not ffmpeg:
        return []

    created = []
    for video in videos:
        if video.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        target = video.with_suffix(".m4a")
        if target.exists():
            created.append(target)
            continue
        for args in (
            [ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "copy", str(target)],
            [ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "aac", str(target)],
        ):
            try:
                result = subprocess.run(args, capture_output=True, timeout=120)
                if result.returncode == 0 and target.exists():
                    created.append(target)
                    break
            except Exception:
                continue
    return created


def _safe_folder(folder: str) -> str:
    """Sanitise a destination folder, allowing nested subfolders.

    Dots are kept, since Instagram handles use them constantly ("3am.aura"),
    but a segment that is only dots after cleaning ("." or "..") is dropped
    rather than kept - that's the one shape that could walk the path back out
    of the downloads directory, at any depth.
    """
    segments = []
    for segment in str(folder).split("/")[:4]:
        clean = "".join(c for c in segment if c.isalnum() or c in " -_.")
        # Strips both ends of whatever mix of spaces/dots sits there. A segment
        # that was nothing *but* dots and spaces (".", "..", " . ") empties out
        # completely here rather than surviving as a lone dot.
        clean = clean.strip(" .")
        if clean:
            segments.append(clean)
    return "/".join(segments) or "saved"


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# The CDN and the API want opposite things: signing a CDN request with the
# API session's headers gets a 404, while a plain anonymous GET succeeds.
_cdn = requests.Session()
_cdn.headers.update({"User-Agent": _BROWSER_UA})


def _fetch_to_file(url: str, dest: Path, stop: threading.Event | None = None) -> int:
    """Stream a CDN URL to disk, unauthenticated."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with _cdn.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if stop is not None and stop.is_set():
                    raise Cancelled()
                handle.write(chunk)
                written += len(chunk)

    if written == 0:
        dest.unlink(missing_ok=True)
    return written


def resolve_audio(shortcode: str) -> dict[str, Any]:
    """Fetch current audio details for one item.

    Stored CDN links are short-lived, so a download re-resolves rather than
    trusting whatever the last sync recorded.
    """
    loader = require_loader()
    media_id = instaloader.Post.shortcode_to_mediaid(shortcode)
    data = loader.context.get_iphone_json(f"api/v1/media/{media_id}/info/", {})
    items = data.get("items") or []
    return _audio_meta(items[0]) if items else {}


def _purge(paths: list[Path], suffixes: set[str]) -> list[Path]:
    """Delete files whose suffix matches, returning what's left."""
    for path in paths:
        if path.suffix.lower() in suffixes:
            path.unlink(missing_ok=True)
    return [p for p in paths if p.exists()]


def download_item(
    shortcode: str,
    folder: str = "saved",
    stop: threading.Event | None = None,
    extract_audio: bool | None = None,
    mode: str = "full",
    audio_url: str = "",
    audio_kind: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download one item, retrying transient failures.

    mode "full" fetches the media as published, filed under `folder`, named for
    the account. mode "audio" keeps only the sound, classified under
    audio/Original Sounds/<account>/ or audio/Licensed Music/<artist>/
    regardless of `folder`: a creator-made track is fetched directly with no
    video transfer at all, while a licensed one falls back to pulling the video
    and extracting its published mix with ffmpeg. Whatever gets downloaded to
    reach that audio - the video, a poster - is always cleaned up, on both
    success and failure, so a failed extraction never leaves debris behind.
    """
    loader = require_loader()
    meta = {**(meta or {}), "shortcode": shortcode}
    base_name = build_name(meta, mode)

    if mode == "audio":
        safe_folder = _safe_folder(audio_destination(audio_kind, meta.get("audio_artist", "")))
    else:
        safe_folder = _safe_folder(folder)
    folder_dir = config.DOWNLOAD_DIR / safe_folder

    # Creator sound: straight download, no video fetched at all.
    if mode == "audio" and audio_kind == "original":
        folder_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(folder_dir, base_name, ".m4a")

        # The stored link may have expired; re-resolve once before giving up.
        for candidate_url, refreshed in ((audio_url, False), (None, True)):
            if stop is not None and stop.is_set():
                raise Cancelled()
            try:
                if refreshed:
                    limiter.wait(stop)
                    candidate_url = (resolve_audio(shortcode) or {}).get("audio_url", "")
                if not candidate_url:
                    continue

                written = _fetch_to_file(candidate_url, dest, stop)
                if written > 0:
                    limiter.relax()
                    return {
                        "shortcode": shortcode, "ok": True, "path": str(folder_dir),
                        "bytes": written, "files": 1, "audio_tracks": 1,
                        "mode": "audio", "filenames": [dest.name],
                    }
            except Cancelled:
                raise
            except Exception as exc:
                if refreshed:
                    events.log(
                        f"{shortcode}: audio fetch failed ({exc}); "
                        f"falling back to the video.",
                        "warn",
                    )
    attempts = max(1, config.MAX_RETRIES)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        if stop is not None and stop.is_set():
            raise Cancelled()

        paths: list[Path] = []
        try:
            limiter.wait(stop)
            post = instaloader.Post.from_shortcode(loader.context, shortcode)

            folder_dir.mkdir(parents=True, exist_ok=True)
            # A literal dirname_pattern writes everything into one flat folder;
            # Instaloader's own naming keeps one item's raw files apart until
            # they're renamed below.
            loader.dirname_pattern = str(folder_dir)
            loader.download_post(post, target=shortcode)

            paths = _item_files(folder_dir, shortcode)
            if not paths:
                raise RuntimeError("Instagram returned no media for this item.")

            paths = _rename_item_files(paths, folder_dir, base_name)

            audio_files: list[Path] = []
            want_audio = (
                mode == "audio"
                or (config.EXTRACT_AUDIO if extract_audio is None else extract_audio)
            )
            if want_audio and post.is_video:
                audio_files = _extract_audio(paths)
                paths = paths + audio_files

            if mode == "audio":
                if not audio_files:
                    reason = (
                        "ffmpeg is not installed, so the audio could not be "
                        "extracted from the video."
                        if not config.ffmpeg_path()
                        else "the audio track could not be extracted."
                    )
                    # Nothing useful came of this attempt - leave no trace,
                    # including any corrupt partial output ffmpeg left behind.
                    _purge(paths, VIDEO_SUFFIXES | IMAGE_SUFFIXES | {".m4a", ".aac"})
                    return {"shortcode": shortcode, "ok": False, "error": reason}
                paths = _purge(paths, VIDEO_SUFFIXES | IMAGE_SUFFIXES)
            elif not config.KEEP_IMAGES:
                paths = _purge(paths, IMAGE_SUFFIXES)

            if not paths:
                # Everything this item had was images, and images aren't kept.
                return {
                    "shortcode": shortcode,
                    "ok": False,
                    "error": "Photo post - images are not being saved "
                             "(set KEEP_IMAGES=true in .env to keep them).",
                }
            size, files = _file_stats(paths)

            limiter.relax()
            return {
                "shortcode": shortcode,
                "ok": True,
                "path": str(folder_dir),
                "bytes": size,
                "files": files,
                "audio_tracks": len(audio_files),
                "mode": mode,
                "filenames": [p.name for p in paths],
            }

        except Cancelled:
            # Don't leave a half-downloaded file behind for a cancelled job.
            # `paths` reflects whatever this attempt last produced - raw
            # Instaloader output if it failed early, renamed/extracted files if
            # it failed late - so cleanup always targets what's really there.
            _purge(paths, VIDEO_SUFFIXES | IMAGE_SUFFIXES | {".m4a", ".aac"})
            raise
        except Exception as exc:
            last_error = exc
            handle_auth_loss(exc)
            _purge(paths, VIDEO_SUFFIXES | IMAGE_SUFFIXES | {".m4a", ".aac"})

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
