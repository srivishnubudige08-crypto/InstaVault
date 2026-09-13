"""Collapse audio/Original Sounds/<account>/ and audio/Licensed Music/<artist>/
subfolders into the two flat buckets, now that per-account/per-artist grouping
has been dropped.

    dry run :  .\\venv\\Scripts\\python.exe scripts\\flatten_audio.py
    apply   :  .\\venv\\Scripts\\python.exe scripts\\flatten_audio.py --apply
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from instavault import client, config, db  # noqa: E402

BUCKETS = ("Original Sounds", "Licensed Music")


def main(apply: bool) -> None:
    audio_root = config.DOWNLOAD_DIR / "audio"
    if not audio_root.exists():
        print("No audio/ folder yet.")
        return

    db.init()
    moved = 0

    for bucket in BUCKETS:
        bucket_dir = audio_root / bucket
        if not bucket_dir.exists():
            continue

        subfolders = [p for p in bucket_dir.iterdir() if p.is_dir()]
        if not subfolders:
            continue
        print(f"\n{bucket}/  ({len(subfolders)} subfolders)")

        for sub in subfolders:
            for file in sorted(sub.iterdir()):
                if not file.is_file():
                    continue

                # Many items share a generic name like "Original audio.m4a"
                # across different account folders, so filename alone isn't
                # unique - match on (source folder, filename) together, which
                # is guaranteed unique since the earlier classification pass
                # never let two files collide within one folder.
                row = db.connection().execute(
                    "SELECT shortcode FROM downloads WHERE path = ? AND filenames LIKE ?",
                    (str(sub), f'%"{file.name}"%'),
                ).fetchone()

                if row:
                    # Recompute the name fresh: an original-sound file now
                    # needs its account folded back into the name, since that
                    # folder is what used to distinguish it.
                    item = db.get_item(row["shortcode"])
                    base = client.build_name(item, "audio")
                else:
                    base = file.stem   # not in the index; keep its name as-is

                target = client._unique_path(bucket_dir, base, file.suffix)
                if moved < 8:
                    print(f"   {file.relative_to(audio_root)}  ->  {target.relative_to(audio_root)}")
                moved += 1

                if apply:
                    file.rename(target)
                    if row:
                        db.record_download(
                            row["shortcode"], "done",
                            path=str(bucket_dir),
                            size=target.stat().st_size, files=1,
                            mode="audio", filenames=[target.name],
                        )

            if apply:
                sub.rmdir()

    verb = "Moved" if apply else "Would move"
    print(f"\n{verb} {moved} file(s)")
    if not apply:
        print("Dry run - nothing changed. Re-run with --apply to do it.")
    else:
        print("Stats now:", db.stats())


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
