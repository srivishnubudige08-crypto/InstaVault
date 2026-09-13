"""Saved-audio listing, classification and export.

Instagram's saved-audio list is mostly licensed catalog: commercially released
recordings it streams under licence. This module reads that list, records what
each entry is, and exports the details - titles, artists, durations - so the
list is portable. It deliberately does not fetch the recordings themselves.

What it does give you is the overlap: which saved tracks are the sound of reels
you already saved, since those reels' audio is downloadable through the normal
item flow.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from . import client, db, events

# Instagram serves the saved-audio list from the same mobile surface as the
# saved-posts feed.
SAVED_AUDIO_PATH = "api/v1/feed/saved/audio/"


def _row_from_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one saved-audio entry, whatever shape it arrives in."""
    # The payload nests the interesting part differently depending on whether
    # the sound is a catalog track or a creator upload.
    asset = (
        entry.get("music_asset_info")
        or (entry.get("music_info") or {}).get("music_asset_info")
        or {}
    )
    original = entry.get("original_sound_info") or {}

    if asset:
        return {
            "kind": "licensed",
            "title": asset.get("title") or "",
            "artist": asset.get("display_artist") or "",
            "duration_ms": asset.get("duration_in_ms"),
            "asset_id": str(asset.get("audio_asset_id") or ""),
            "cover_url": asset.get("cover_artwork_thumbnail_uri") or "",
            "link": f"https://www.instagram.com/reels/audio/{asset.get('audio_asset_id')}/",
        }

    if original:
        artist = (original.get("ig_artist") or {}).get("username", "")
        asset_id = original.get("audio_asset_id")
        return {
            "kind": "original",
            "title": original.get("original_audio_title") or "",
            "artist": artist,
            "duration_ms": original.get("duration_in_ms"),
            "asset_id": str(asset_id or ""),
            "cover_url": "",
            "link": f"https://www.instagram.com/reels/audio/{asset_id}/",
        }

    return None


def fetch_saved_audio(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the saved-audio list, paginating until exhausted.

    Raises AuthError-ish exceptions via client.describe() so the caller can show
    the same messages as everything else.
    """
    loader = client.require_loader()
    context = loader.context

    rows: list[dict[str, Any]] = []
    max_id: str | None = None

    while True:
        client.limiter.wait()
        params: dict[str, Any] = {}
        if max_id:
            params["max_id"] = max_id

        try:
            data = context.get_iphone_json(SAVED_AUDIO_PATH, params)
        except Exception as exc:
            client.handle_auth_loss(exc)
            raise client.describe(exc) from exc

        for entry in data.get("items", []):
            row = _row_from_entry(entry.get("audio") or entry)
            if row:
                rows.append(row)
                if limit is not None and len(rows) >= limit:
                    return rows

        client.limiter.relax()
        if not data.get("more_available"):
            break
        max_id = data.get("next_max_id")
        if not max_id:
            break

    events.log(f"Read {len(rows)} saved audio track(s).")
    return rows


def tracks_from_index(**filters: Any) -> list[dict[str, Any]]:
    """Distinct tracks across the saved reels already indexed.

    Built entirely from local data, so it works regardless of whether
    Instagram's saved-audio endpoint is reachable. One row per track, with the
    reels that use it.
    """
    filters.setdefault("audio", "any_audio")
    grouped: dict[str, dict[str, Any]] = {}

    for item in db.query_items(limit=100_000, **filters):
        kind = item.get("audio_kind") or ""
        if not kind:
            continue

        # Instagram sometimes returns "0" for the asset id, which is no id at
        # all - grouping on it would collapse unrelated sounds into one row.
        asset_id = (item.get("audio_asset_id") or "").strip()
        if asset_id in {"", "0"}:
            asset_id = ""

        title = (item.get("audio_title") or "").strip()
        artist = (item.get("audio_artist") or "").strip()

        if asset_id:
            key = f"id:{asset_id}"
        elif title or artist:
            key = f"name:{title}|{artist}"
        else:
            # Nothing to identify it by; keep it as its own row rather than
            # pooling every unlabelled sound together.
            key = f"item:{item['shortcode']}"
        track = grouped.setdefault(
            key,
            {
                "title": title or "(untitled sound)",
                "artist": artist,
                "kind": "original" if kind == "original" else "licensed",
                "asset_id": asset_id,
                "duration_ms": None,
                "reel_count": 0,
                "reels": [],
                "link": (
                    f"https://www.instagram.com/reels/audio/{asset_id}/" if asset_id else ""
                ),
            },
        )
        track["reel_count"] += 1
        track["reels"].append({"shortcode": item["shortcode"], "owner": item.get("owner", "")})

    return sorted(grouped.values(), key=lambda t: (-t["reel_count"], t["title"].lower()))


def overlap_report(tracks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Match saved tracks against the sounds of reels already in the index.

    Joins on audio_asset_id, which both sides expose. This reads two things we
    already hold - it does not go looking for reels the user has not saved.
    """
    tracks = tracks if tracks is not None else fetch_saved_audio()

    by_asset: dict[str, list[dict[str, str]]] = {}
    for item in db.query_items(limit=100_000, audio="any_audio"):
        asset_id = item.get("audio_asset_id") or ""
        if asset_id:
            by_asset.setdefault(asset_id, []).append(
                {"shortcode": item["shortcode"], "owner": item.get("owner", "")}
            )

    report = []
    for track in tracks:
        reels = by_asset.get(track.get("asset_id", ""), [])
        report.append({**track, "reel_count": len(reels), "reels": reels})
    return report


def to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    fields = ["title", "artist", "kind", "duration_ms", "asset_id", "reel_count", "link"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False)
