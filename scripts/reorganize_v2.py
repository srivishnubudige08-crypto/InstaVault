"""Apply the new naming and audio classification to existing downloads.

Two passes:
  1. Delete orphaned debris under audio/ - a successful audio download only
     ever keeps an .m4a/.aac/.mp3, so anything else there (leftover video,
     poster images from failed extractions before ffmpeg was found) is safe to
     remove unconditionally.
  2. Rename every remaining file to the new scheme and, for audio, move it into
     its classification folder (audio/Original Sounds/<account>/ or
     audio/Licensed Music/<artist>/). Updates the index (path, filenames, mode)
     to match.

    dry run :  .\\venv\\Scripts\\python.exe scripts\\reorganize_v2.py
    apply   :  .\\venv\\Scripts\\python.exe scripts\\reorganize_v2.py --apply
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import client, config, db  # noqa: E402

# The old scheme carried "[shortcode]" somewhere in the name - usually at the
# very end, but a handful acquired a " (2)" collision suffix after it during
# an earlier run. New-style names never contain brackets at all, so matching
# anywhere is safe and catches both shapes with one pattern.
OLD_NAME = re.compile(r"\[(?P<shortcode>[^\[\]]+)\]")
AUDIO_SUFFIXES = {".m4a", ".aac", ".mp3"}


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clean_audio_debris(apply: bool) -> None:
    audio_root = config.DOWNLOAD_DIR / "audio"
    if not audio_root.exists():
        return

    debris = [
        p for p in audio_root.rglob("*")
        if p.is_file() and p.suffix.lower() not in AUDIO_SUFFIXES
    ]
    freed = sum(p.stat().st_size for p in debris)
    verb = "Deleted" if apply else "Would delete"
    print(f"\n=== Pass 1: orphaned debris under audio/ ===")
    print(f"{verb} {len(debris)} file(s), freeing {human(freed)}")

    if apply:
        for p in debris:
            p.unlink(missing_ok=True)


def reorganize(apply: bool) -> None:
    root = config.DOWNLOAD_DIR
    db.init()

    moved = skipped = unknown = 0
    print(f"\n=== Pass 2: rename + classify ===")

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        match = OLD_NAME.search(path.stem)
        if not match:
            skipped += 1
            continue

        shortcode = match.group("shortcode")
        item = db.get_item(shortcode)
        if not item:
            unknown += 1
            continue

        is_audio = path.suffix.lower() in AUDIO_SUFFIXES and "audio" in path.parts
        mode = "audio" if is_audio else "full"

        if mode == "audio":
            rel_folder = client.audio_destination(
                item.get("audio_kind") or "", item.get("audio_artist") or ""
            )
        else:
            # Stay in whichever collection folder it's already in.
            rel_folder = str(path.parent.relative_to(root)).replace("\\", "/")

        target_dir = root / client._safe_folder(rel_folder)
        base_name = client.build_name(item, mode)
        target = client._unique_path(target_dir, base_name, path.suffix)

        if moved < 8:
            print(f"   {path.relative_to(root)}")
            print(f"     -> {target.relative_to(root)}")
        moved += 1

        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            path.rename(target)
            size = target.stat().st_size
            db.record_download(
                shortcode, "done",
                path=str(target_dir), size=size, files=1,
                mode=mode, filenames=[target.name],
            )

    verb = "Moved" if apply else "Would move"
    print(f"\n{verb} {moved} file(s)")
    print(f"Left alone (already new-style, or not ours): {skipped}")
    if unknown:
        print(f"Not in the index (left as-is): {unknown}")

    if apply:
        print("\nStats now:", db.stats())
    else:
        print("\nDry run - nothing changed. Re-run with --apply to do it.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    clean_audio_debris(apply)
    reorganize(apply)
