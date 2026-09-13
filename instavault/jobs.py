"""Background work: syncing the saved feed and downloading media.

Everything slow runs on a single worker thread. One at a time is deliberate -
running two jobs against Instagram at once is the fastest way to get an account
rate limited. Progress is pushed to the browser as it happens, and every job can
be cancelled mid-flight.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import client, config, db, events


@dataclass
class Job:
    kind: str                       # sync | download
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"          # queued | running | done | failed | cancelled
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    current: str = ""
    detail: str = ""
    error: str = ""
    bytes: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return max(end - self.started_at, 0.0)

    @property
    def rate(self) -> float:
        """Items per second, once there is enough signal to mean anything."""
        done = self.completed + self.failed + self.skipped
        return done / self.elapsed if self.elapsed > 1 and done else 0.0

    @property
    def eta(self) -> float | None:
        if self.total <= 0 or not self.rate:
            return None
        remaining = self.total - (self.completed + self.failed + self.skipped)
        return max(remaining, 0) / self.rate if remaining > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        done = self.completed + self.failed + self.skipped
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "processed": done,
            "percent": round(done / self.total * 100, 1) if self.total else 0.0,
            "current": self.current,
            "detail": self.detail,
            "error": self.error,
            "bytes": self.bytes,
            "elapsed": round(self.elapsed, 1),
            "rate": round(self.rate, 2),
            "eta": round(self.eta) if self.eta is not None else None,
            "cancellable": self.status in {"queued", "running"},
        }


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Job | None = None
        self._history: list[Job] = []
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.status == "running"

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return self._current.as_dict() if self._current else None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return [j.as_dict() for j in self._history[-limit:][::-1]]

    def cancel(self, job_id: str = "") -> bool:
        with self._lock:
            job = self._current
            if job is None or (job_id and job.id != job_id):
                return False
            if job.status not in {"queued", "running"}:
                return False
            job.stop.set()
            job.detail = "Stopping after the current item..."
        self._emit(job)
        events.log("Cancelling - finishing the item in flight.", "warn")
        return True

    # ------------------------------------------------------------------ launch

    def _start(self, job: Job, target: Callable[[Job], None]) -> Job:
        with self._lock:
            if self.busy:
                raise RuntimeError("Another job is already running.")
            self._current = job
            self._history.append(job)
            del self._history[:-25]

        def runner() -> None:
            job.status = "running"
            job.started_at = time.monotonic()
            self._emit(job)
            try:
                target(job)
                job.status = "cancelled" if job.stop.is_set() else "done"
            except client.Cancelled:
                job.status = "cancelled"
            except client.AuthError as exc:
                job.status = "failed"
                job.error = exc.message
                events.publish("auth_error", **exc.as_dict())
            except Exception as exc:
                job.status = "failed"
                job.error = client.describe(exc).message
                events.log(f"Job failed: {job.error}", "error")
            finally:
                job.finished_at = time.monotonic()
                job.current = ""
                job.detail = self._summary(job)
                self._emit(job)
                events.publish("stats", **db.stats())
                with self._lock:
                    if self._current is job:
                        self._current = None

        self._thread = threading.Thread(target=runner, name=f"job-{job.id}", daemon=True)
        self._thread.start()
        return job

    @staticmethod
    def _summary(job: Job) -> str:
        if job.status == "failed":
            return job.error or "Failed."
        parts = []
        if job.kind == "sync":
            parts.append(f"{job.completed} item{'s' if job.completed != 1 else ''} indexed")
        else:
            parts.append(f"{job.completed} downloaded")
            if job.skipped:
                parts.append(f"{job.skipped} already had")
        if job.failed:
            parts.append(f"{job.failed} failed")
        if job.status == "cancelled":
            parts.append("stopped early")
        return ", ".join(parts) + "."

    def _emit(self, job: Job) -> None:
        events.publish("job", job=job.as_dict())

    # -------------------------------------------------------------- job bodies

    def start_sync(self, limit: int | None = None) -> Job:
        job = Job(kind="sync")

        def body(j: Job) -> None:
            events.log("Reading your saved feed...")
            seen: set[str] = set()
            # The feed length is unknown until it is walked, so total tracks the
            # count discovered so far and percent stays honest by staying at 0.
            j.total = limit or 0

            def flush(rows: list[dict[str, Any]]) -> None:
                new = db.upsert_items(rows)
                j.completed += len(rows)
                j.detail = f"{j.completed} indexed ({new} new in last batch)"
                self._emit(j)
                events.publish("items_added", count=len(rows), total=j.completed)
                events.publish("stats", **db.stats())

            for row in client.iter_saved(stop=j.stop, limit=limit, on_page=flush):
                seen.add(row["shortcode"])
                j.current = row["shortcode"]

            if not j.stop.is_set() and limit is None and seen:
                removed = db.mark_unsaved(seen)
                if removed:
                    events.log(f"{removed} item(s) are no longer in your saved feed.")

            db.set_meta("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            events.log(f"Sync finished - {j.completed} item(s).", "success")

        return self._start(job, body)

    def start_download(
        self,
        shortcodes: list[str],
        folder: str = "saved",
        extract_audio: bool | None = None,
        skip_existing: bool = True,
        mode: str = "full",
    ) -> Job:
        job = Job(kind="download")
        job.total = len(shortcodes)

        def body(j: Job) -> None:
            what = "audio" if mode == "audio" else "item"
            events.log(f"Downloading {what} for {j.total} item(s) into '{folder}'.")

            for shortcode in shortcodes:
                if j.stop.is_set():
                    raise client.Cancelled()

                if skip_existing and db.is_downloaded(shortcode, mode):
                    j.skipped += 1
                    j.detail = f"Skipped {shortcode} - already downloaded"
                    self._emit(j)
                    events.publish("item", shortcode=shortcode, status="skipped")
                    continue

                j.current = shortcode
                j.detail = f"Downloading {shortcode}"
                self._emit(j)

                # The index knows which route this item's audio takes.
                item = db.get_item(shortcode) or {}
                result = client.download_item(
                    shortcode,
                    folder=folder,
                    stop=j.stop,
                    extract_audio=extract_audio,
                    mode=mode,
                    audio_url=item.get("audio_url") or "",
                    audio_kind=item.get("audio_kind") or "",
                )

                if result.get("ok"):
                    j.completed += 1
                    j.bytes += result.get("bytes", 0)
                    db.record_download(
                        shortcode,
                        "done",
                        path=result.get("path", ""),
                        size=result.get("bytes", 0),
                        files=result.get("files", 0),
                        mode=mode,
                    )
                    events.publish(
                        "item", shortcode=shortcode, status="done",
                        bytes=result.get("bytes", 0),
                    )
                else:
                    j.failed += 1
                    error = result.get("error", "Unknown error")
                    db.record_download(shortcode, "failed", error=error, mode=mode)
                    events.publish("item", shortcode=shortcode, status="failed", error=error)
                    events.log(f"{shortcode}: {error}", "error")

                self._emit(j)
                events.publish("stats", **db.stats())

            events.log(self._summary(j), "success" if not j.failed else "warn")

        return self._start(job, body)


manager = JobManager()
