"""Flatten per-item folders and drop images from existing downloads.

Moves every file up into its collection folder, removes image files, deletes the
emptied item folders, and repoints the index at the new locations.

    dry run :  .\\venv\\Scripts\\python.exe scripts\\flatten_downloads.py
    apply   :  .\\venv\\Scripts\\python.exe scripts\\flatten_downloads.py --apply
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import config, db  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main(apply: bool) -> None:
    root = config.DOWNLOAD_DIR
    if not root.exists():
        print(f"No downloads folder at {root}")
        return

    moved = deleted = folders = 0
    freed = 0
    collisions: list[str] = []

    for collection in sorted(p for p in root.iterdir() if p.is_dir()):
        item_dirs = [p for p in collection.iterdir() if p.is_dir()]
        if not item_dirs:
            continue
        print(f"\n{collection.name}/  ({len(item_dirs)} item folders)")

        for item_dir in item_dirs:
            for file in sorted(p for p in item_dir.rglob("*") if p.is_file()):
                if file.suffix.lower() in IMAGE_SUFFIXES:
                    freed += file.stat().st_size
                    deleted += 1
                    if apply:
                        file.unlink(missing_ok=True)
                    continue

                target = collection / file.name
                if target.exists() and target.resolve() != file.resolve():
                    # Shouldn't happen (names carry the shortcode) but never
                    # silently overwrite.
                    collisions.append(str(file))
                    continue
                moved += 1
                if apply:
                    shutil.move(str(file), str(target))

            folders += 1
            if apply:
                shutil.rmtree(item_dir, ignore_errors=True)

    verb = "Moved" if apply else "Would move"
    verb2 = "Deleted" if apply else "Would delete"
    print()
    print(f"{verb} {moved} media file(s) up into their collection folder")
    print(f"{verb2} {deleted} image(s), freeing {human(freed)}")
    print(f"{verb2} {folders} now-empty item folder(s)")
    if collisions:
        print(f"SKIPPED {len(collisions)} file(s) that would overwrite something:")
        for c in collisions[:10]:
            print("   ", c)

    if not apply:
        print("\nDry run - nothing changed. Re-run with --apply to do it.")
        return

    # Repoint the index at the collection folder.
    db.init()
    with db.transaction() as conn:
        updated = conn.execute(
            "UPDATE downloads SET path = ? WHERE path LIKE ?",
            (str(root), "%"),
        ).rowcount
    print(f"\nIndex updated: {updated} download row(s) repointed.")

    # Recompute stored bytes per item from what actually survives on disk.
    fixed = 0
    with db.transaction() as conn:
        for row in conn.execute("SELECT shortcode FROM downloads WHERE status='done'"):
            sc = row["shortcode"]
            total = 0
            for collection in (p for p in root.iterdir() if p.is_dir()):
                for f in collection.glob(f"{sc}*"):
                    if f.is_file():
                        total += f.stat().st_size
            conn.execute(
                "UPDATE downloads SET bytes = ?, path = ? WHERE shortcode = ?",
                (total, str(root), sc),
            )
            fixed += 1
    print(f"Recomputed sizes for {fixed} item(s).")
    print("Stats now:", db.stats())


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
