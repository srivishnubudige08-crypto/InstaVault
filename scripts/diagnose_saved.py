"""One-shot diagnostic: hit the saved-feed endpoints with the cached session and
print exactly what Instagram returns, so we stop guessing at the response shape.

Run: .\venv\Scripts\python.exe scripts\diagnose_saved.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import client, config  # noqa: E402


def show(label, obj, depth=0):
    if isinstance(obj, dict):
        print("  " * depth + f"{label}: dict keys = {list(obj.keys())[:12]}")
    elif isinstance(obj, list):
        print("  " * depth + f"{label}: list len = {len(obj)}")
    else:
        text = repr(obj)
        print("  " * depth + f"{label}: {text[:80]}")


def main():
    username = config.USERNAME
    if not username:
        # fall back to whatever session file exists
        sessions = list(config.SESSION_DIR.glob("*.session"))
        if not sessions:
            print("No cached session. Sign in through the app first.")
            return
        username = sessions[0].stem

    print(f"Restoring session for: {username}")
    if not client.restore(username):
        print("Session could not be restored / is invalid. Re-sign-in in the app.")
        return

    ctx = client.session.loader.context
    print(f"Logged-in context user_id: {getattr(ctx, 'user_id', '?')}")
    print()

    for path in ("api/v1/feed/saved/posts/", "api/v1/feed/saved/"):
        print(f"=== GET {path} ===")
        try:
            data = ctx.get_iphone_json(path, {})
        except Exception as exc:
            print(f"  RAISED {type(exc).__name__}: {exc}")
            print()
            continue

        show("top-level", data)
        for key in ("status", "more_available", "next_max_id", "num_results", "total_count"):
            if key in data:
                print(f"    {key} = {data[key]!r}")

        items = data.get("items")
        if isinstance(items, list):
            print(f"    items: {len(items)}")
            if items:
                first = items[0]
                show("items[0]", first, depth=2)
                media = first.get("media") if isinstance(first, dict) else None
                if isinstance(media, dict):
                    print(f"      media keys: {list(media.keys())[:16]}")
                    for k in ("code", "media_type", "taken_at"):
                        print(f"        media.{k} = {media.get(k)!r}")
        print()


if __name__ == "__main__":
    main()
