"""Run a complete saved-feed sync into the local index."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from instavault import client, db, config

db.init()
username = config.USERNAME or next(p.stem for p in config.SESSION_DIR.glob("*.session"))
if not client.restore(username):
    print("Session invalid — sign in again in the app."); sys.exit(1)

print(f"Syncing everything saved by {username}...", flush=True)
total = 0
t0 = time.time()

def flush(rows):
    global total
    new = db.upsert_items(rows)
    total += len(rows)
    print(f"  indexed {total} (+{new} new)  {time.time()-t0:.0f}s", flush=True)

seen = set()
try:
    for row in client.iter_saved(on_page=flush):
        seen.add(row["shortcode"])
except Exception as e:
    print(f"stopped: {type(e).__name__}: {e}", flush=True)

if seen:
    db.mark_unsaved(seen)
print(f"DONE: {total} items in {time.time()-t0:.0f}s", flush=True)
print("stats:", db.stats(), flush=True)
print("collections:", db.collections(), flush=True)
