"""Instagram session handling and saved-item access.

Wraps Instaloader so the rest of the app never touches it directly. Login uses a
cached session file, so a password is needed at most once per machine.
"""

import time
from dataclasses import dataclass
from pathlib import Path

import instaloader

from . import config


class LoginRequired(Exception):
    """Raised when no valid cached session exists for the configured user."""


@dataclass
class SavedItem:
    """One saved post, reel or video."""

    shortcode: str
    typename: str          # GraphImage | GraphVideo | GraphSidecar
    is_video: bool
    caption: str
    owner: str
    thumbnail_url: str
    date: str


def _session_file(username: str) -> Path:
    return config.SESSION_DIR / f"{username}.session"


def build_loader() -> instaloader.Instaloader:
    """An Instaloader configured for polite, rate-limited use."""
    config.ensure_dirs()
    return instaloader.Instaloader(
        dirname_pattern=str(config.DOWNLOAD_DIR / "{target}"),
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )


def load_session(username: str) -> instaloader.Instaloader:
    """Restore a cached session. Raises LoginRequired if none exists."""
    loader = build_loader()
    path = _session_file(username)
    if not path.exists():
        raise LoginRequired(f"No cached session for {username!r}")
    loader.load_session_from_file(username, str(path))
    return loader


def login(username: str, password: str) -> instaloader.Instaloader:
    """Log in and cache the session for reuse."""
    config.ensure_dirs()
    loader = build_loader()
    loader.login(username, password)
    loader.save_session_to_file(str(_session_file(username)))
    return loader


def fetch_saved(loader: instaloader.Instaloader, limit: int | None = None) -> list[SavedItem]:
    """List items from the logged-in account's Saved feed."""
    profile = instaloader.Profile.own_profile(loader.context)
    items: list[SavedItem] = []

    for index, post in enumerate(profile.get_saved_posts()):
        if limit is not None and index >= limit:
            break
        items.append(
            SavedItem(
                shortcode=post.shortcode,
                typename=post.typename,
                is_video=post.is_video,
                caption=(post.caption or "")[:280],
                owner=post.owner_username,
                thumbnail_url=post.url,
                date=post.date_utc.isoformat(),
            )
        )
        time.sleep(config.REQUEST_DELAY)

    return items


def download(loader: instaloader.Instaloader, shortcode: str, target: str = "saved") -> Path:
    """Download a single saved item into DOWNLOAD_DIR/<target>/."""
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    loader.download_post(post, target=target)
    time.sleep(config.REQUEST_DELAY)
    return config.DOWNLOAD_DIR / target
