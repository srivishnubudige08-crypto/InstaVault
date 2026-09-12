"""Inspect collections and saved-audio endpoints with the cached session."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import client, config  # noqa: E402


def main():
    username = config.USERNAME or next(p.stem for p in config.SESSION_DIR.glob("*.session"))
    if not client.restore(username):
        print("Restore failed — re-sign-in in the app.")
        return
    ctx = client.session.loader.context

    print("=== GET api/v1/collections/list/ ===")
    try:
        data = ctx.get_iphone_json(
            "api/v1/collections/list/",
            {"collection_types": '["ALL_MEDIA_AUTO_COLLECTION","PRODUCT_AUTO_COLLECTION","MEDIA","AUDIO_AUTO_COLLECTION"]'},
        )
        print("top keys:", list(data.keys()))
        for item in data.get("items", []):
            print(
                f"  - id={item.get('collection_id')!r} "
                f"type={item.get('collection_type')!r} "
                f"name={item.get('collection_name')!r} "
                f"count={item.get('collection_media_count')!r} "
                f"cover={'yes' if item.get('cover_media') else 'no'}"
            )
        print("  more_available:", data.get("more_available"), "next:", bool(data.get("next_max_id")))
    except Exception as exc:
        print(f"  RAISED {type(exc).__name__}: {exc}")

    print()
    print("=== total saved count via full walk (no download) ===")
    try:
        n = sum(1 for _ in client.iter_saved())
        print(f"  total saved posts: {n}")
    except Exception as exc:
        print(f"  RAISED {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
