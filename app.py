"""InstaVault - local dashboard for your own saved Instagram items."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
)

from instavault import audio, client, config, db, events, jobs

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_TYPES = {".mp4", ".mov"}


def _error(message: str, status: int = 400, **extra):
    return jsonify({"error": message, **extra}), status


@app.errorhandler(client.AuthError)
def _auth_error(exc: client.AuthError):
    status = 401 if exc.code in {"unauthenticated", "session_expired"} else 400
    return jsonify(exc.as_dict()), status


# ------------------------------------------------------------------------- page


@app.route("/")
def index():
    return render_template("index.html", version=_version())


def _version() -> str:
    from instavault import __version__

    return __version__


# ------------------------------------------------------------------------- auth


@app.get("/api/status")
def status():
    return jsonify(
        {
            "auth": client.session.as_dict(),
            "stats": db.stats(),
            "job": jobs.manager.current(),
            "recent_jobs": jobs.manager.recent(5),
            "collections": db.collections(),
            "last_sync": db.get_meta("last_sync"),
            "settings": {
                "download_dir": str(config.DOWNLOAD_DIR),
                "request_delay": config.REQUEST_DELAY,
                "max_retries": config.MAX_RETRIES,
                "extract_audio": config.EXTRACT_AUDIO,
                "ffmpeg": bool(config.ffmpeg_path()),
            },
        }
    )


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or config.USERNAME).strip()
    password = payload.get("password") or config.PASSWORD

    if not username:
        return _error("Enter your Instagram username.")

    # A cached session means no password is needed at all.
    if not password and client.restore(username):
        return jsonify({"authenticated": True, "username": username, "from_cache": True})

    if not password:
        return _error("Enter your password, or sign in once to cache a session.")

    return jsonify(client.login(username, password))


@app.post("/api/login-session")
def login_session():
    """Sign in by adopting a browser session cookie, skipping password login."""
    payload = request.get_json(silent=True) or {}
    return jsonify(
        client.login_with_session_cookie(
            sessionid=payload.get("sessionid") or "",
            username=(payload.get("username") or "").strip(),
            csrftoken=payload.get("csrftoken") or "",
        )
    )


@app.post("/api/two-factor")
def two_factor():
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    if not code:
        return _error("Enter the 6-digit code.")
    return jsonify(client.two_factor(code))


@app.post("/api/logout")
def logout():
    payload = request.get_json(silent=True) or {}
    client.logout(forget=bool(payload.get("forget")))
    return jsonify(client.session.as_dict())


# ------------------------------------------------------------------------ items


def _filter_args():
    """Pull the shared filter set off the query string."""
    args = request.args
    return {
        "search": args.get("search", "").strip(),
        "kind": args.get("kind", "all"),
        "state": args.get("state", "all"),
        "collection": args.get("collection", "").strip(),
        "audio": args.get("audio", "any"),
        "artist": args.get("artist", "").strip(),
        "owner": args.get("owner", "").strip(),
        "duration": args.get("duration", "any"),
    }


@app.get("/api/items")
def items():
    args = request.args
    filters = _filter_args()
    limit = min(args.get("limit", type=int, default=60), 500)
    offset = max(args.get("offset", type=int, default=0), 0)

    rows = db.query_items(sort=args.get("sort", "newest"), limit=limit, offset=offset, **filters)
    total = db.count_items(**filters)

    return jsonify(
        {
            "items": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total,
        }
    )


@app.get("/api/items/all")
def item_ids():
    """Every shortcode matching the current filter, for 'select all matching'."""
    return jsonify({"shortcodes": db.all_shortcodes(**_filter_args())})


@app.get("/api/artists")
def artists():
    """Artists present in the index, for the filter's autocomplete."""
    return jsonify({"artists": db.artists()})


@app.get("/api/stats")
def stats():
    return jsonify(db.stats())


@app.get("/api/audio")
def saved_audio():
    """Tracks across the saved reels, classified as creator-original or licensed.

    Sourced from the local index rather than Instagram's saved-audio endpoint,
    which refuses cookie-session logins.
    """
    tracks = audio.tracks_from_index(**_filter_args())
    return jsonify(
        {
            "tracks": tracks,
            "count": len(tracks),
            "original": sum(1 for t in tracks if t["kind"] == "original"),
            "licensed": sum(1 for t in tracks if t["kind"] == "licensed"),
        }
    )


@app.get("/api/audio/overlap")
def audio_overlap():
    """Which saved tracks are the sound of reels already in the index."""
    client.require_loader()
    report = audio.overlap_report()
    matched = [r for r in report if r["reel_count"]]
    return jsonify(
        {
            "tracks": report,
            "total": len(report),
            "matched": len(matched),
            "reels": sum(r["reel_count"] for r in report),
        }
    )


@app.get("/api/audio/export")
def audio_export():
    """Download the audio list as CSV or JSON.

    Built from the local index, so it works offline and regardless of whether
    Instagram's saved-audio endpoint is reachable - which it generally is not.
    """
    fmt = request.args.get("fmt", "csv").lower()
    if fmt not in {"csv", "json"}:
        return _error("fmt must be 'csv' or 'json'.")

    rows = audio.tracks_from_index(**_filter_args())
    kind = request.args.get("kind", "")          # original | licensed | ''
    if kind in {"original", "licensed"}:
        rows = [r for r in rows if r["kind"] == kind]

    body = audio.to_csv(rows) if fmt == "csv" else audio.to_json(rows)
    mime = "text/csv" if fmt == "csv" else "application/json"
    return Response(
        body,
        mimetype=mime,
        headers={
            "Content-Disposition": f'attachment; filename="instavault-audio.{fmt}"'
        },
    )


@app.post("/api/collections/rename")
def rename_collection():
    payload = request.get_json(silent=True) or {}
    cid = (payload.get("id") or "").strip()
    if not cid:
        return _error("Missing collection id.")
    db.rename_collection(cid, payload.get("name") or "")
    return jsonify({"collections": db.collections()})


# ------------------------------------------------------------------------- jobs


@app.post("/api/sync")
def sync():
    payload = request.get_json(silent=True) or {}
    client.require_loader()
    try:
        job = jobs.manager.start_sync(limit=payload.get("limit"))
    except RuntimeError as exc:
        return _error(str(exc), 409, job=jobs.manager.current())
    return jsonify(job.as_dict())


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    shortcodes = payload.get("shortcodes") or []
    if not shortcodes:
        return _error("Select at least one item.")

    mode = payload.get("mode") or "full"
    if mode not in {"full", "audio"}:
        return _error("mode must be 'full' or 'audio'.")

    client.require_loader()
    try:
        job = jobs.manager.start_download(
            shortcodes=[str(s) for s in shortcodes],
            folder=payload.get("folder") or "saved",
            extract_audio=payload.get("extract_audio"),
            skip_existing=payload.get("skip_existing", True),
            mode=mode,
        )
    except RuntimeError as exc:
        return _error(str(exc), 409, job=jobs.manager.current())
    return jsonify(job.as_dict())


@app.post("/api/cancel")
def cancel():
    payload = request.get_json(silent=True) or {}
    ok = jobs.manager.cancel(payload.get("job_id", ""))
    if not ok:
        return _error("Nothing running to cancel.", 409)
    return jsonify({"cancelling": True})


@app.get("/api/events")
def event_stream():
    return Response(
        stream_with_context(events.stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/log")
def log_history():
    return jsonify(events.history())


# ------------------------------------------------------------------------ media


@app.get("/api/thumb/<shortcode>")
def thumb(shortcode: str):
    """Proxy and cache thumbnails.

    Instagram's CDN URLs expire and block cross-origin hotlinking, so the
    browser cannot use them directly - we fetch once and serve from disk after.
    """
    config.ensure_dirs()
    cached = config.THUMB_DIR / f"{shortcode}.jpg"

    if cached.exists() and cached.stat().st_size > 0:
        return send_file(cached, mimetype="image/jpeg", max_age=86400)

    item = db.get_item(shortcode)
    if not item or not item.get("thumb_url"):
        return _error("No thumbnail on record.", 404)

    try:
        response = requests.get(
            item["thumb_url"],
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.instagram.com/"},
        )
        response.raise_for_status()
        cached.write_bytes(response.content)
    except Exception:
        # Expired URL - a re-sync refreshes it.
        return _error("Thumbnail expired. Re-sync to refresh.", 404, stale=True)

    return send_file(cached, mimetype="image/jpeg", max_age=86400)


@app.get("/api/media/<shortcode>")
def media(shortcode: str):
    """Serve a downloaded file so the lightbox can play local copies.

    Uses the exact filenames recorded at download time rather than scanning the
    folder - collection folders hold many items' files side by side, so a
    folder-wide scan would serve whichever file happened to be biggest, not
    necessarily this item's.
    """
    item = db.get_item(shortcode)
    if not item or not item.get("download_path"):
        return _error("Not downloaded yet.", 404)

    folder = Path(item["download_path"])
    names = json.loads(item.get("download_filenames") or "[]")
    candidates = [folder / name for name in names if (folder / name).is_file()]
    if not candidates:
        return _error("Downloaded file is missing from disk.", 404)

    wanted = VIDEO_TYPES if item.get("is_video") else IMAGE_TYPES
    media_files = [p for p in candidates if p.suffix.lower() in wanted] or candidates
    target = max(media_files, key=lambda p: p.stat().st_size)

    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return send_file(target, mimetype=mime, conditional=True)


@app.post("/api/reveal")
def reveal():
    """Open the download folder in the OS file manager."""
    payload = request.get_json(silent=True) or {}
    target = Path(payload.get("path") or config.DOWNLOAD_DIR)

    try:
        target = target.resolve()
        target.relative_to(config.DOWNLOAD_DIR.resolve())
    except ValueError:
        return _error("That path is outside the download folder.", 403)
    except Exception:
        return _error("Could not resolve that path.", 400)

    if not target.exists():
        return _error("That folder does not exist yet.", 404)

    try:
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
    except Exception as exc:
        return _error(f"Could not open the folder: {exc}", 500)

    return jsonify({"opened": str(target)})


# ------------------------------------------------------------------------- boot


def bootstrap() -> None:
    config.ensure_dirs()
    db.init()
    if config.USERNAME and client.restore(config.USERNAME):
        print(f" * Restored cached session for {config.USERNAME}")


if __name__ == "__main__":
    bootstrap()
    url = f"http://{config.HOST}:{config.PORT}"
    print(f" * InstaVault ready at {url}")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, threaded=True)
