"""Rename already-downloaded files to the readable scheme, and drop any images.

Old names look like ``Dc6N7gBv4_w_2026-09-05_13-01-09.mp4``; new ones carry the
account, date and caption (or artist and track, for audio) with the shortcode
kept in brackets so the index can still find them.

    dry run :  .\\venv\\Scripts\\python.exe scripts\\rename_downloads.py
    apply   :  .\\venv\\Scripts\\python.exe scripts\\rename_downloads.py --apply
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import client, config, db  # noqa: E402

# Instaloader's pattern was "{shortcode}_{date}_{time}". Anchoring on the date
# keeps shortcodes that themselves contain underscores intact.
OLD_NAME = re.compile(r"^(?P<shortcode>.+?)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
AUDIO_SUFFIXES = {".m4a", ".aac", ".mp3"}


def main(apply: bool) -> None:
    root = config.DOWNLOAD_DIR
    if not root.exists():
        print(f"No downloads folder at {root}")
        return

    db.init()
    renamed = skipped = removed = unknown = 0

    for collection in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(p for p in collection.rglob("*") if p.is_file())
        if not files:
            continue
        print(f"\n{collection.name}/  ({len(files)} files)")

        for path in files:
            if path.suffix.lower() in client.IMAGE_SUFFIXES:
                removed += 1
                if apply:
                    path.unlink(missing_ok=True)
                continue

            match = OLD_NAME.match(path.stem)
            if not match:
                skipped += 1          # already renamed, or not ours
                continue

            shortcode = match.group("shortcode")
            item = db.get_item(shortcode)
            if not item:
                unknown += 1
                continue

            mode = "audio" if path.suffix.lower() in AUDIO_SUFFIXES else "full"
            base = client.build_name(item, mode)
            target = path.with_name(f"{base}{path.suffix}")
            if target == path:
                skipped += 1
                continue

            counter = 2
            while target.exists():
                target = path.with_name(f"{base} ({counter}){path.suffix}")
                counter += 1

            if renamed < 5:
                print(f"   {path.name}")
                print(f"     -> {target.name}")
            renamed += 1
            if apply:
                try:
                    path.rename(target)
                except OSError as exc:
                    print(f"   could not rename {path.name}: {exc}")
                    renamed -= 1

    verb = "Renamed" if apply else "Would rename"
    print()
    print(f"{verb} {renamed} file(s)")
    if removed:
        print(f"{'Deleted' if apply else 'Would delete'} {removed} image(s)")
    if skipped:
        print(f"Left alone: {skipped} (already named, or unrecognised)")
    if unknown:
        print(f"Not in the index: {unknown} (left as-is)")

    if not apply:
        print("\nDry run - nothing changed. Re-run with --apply to do it.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
