"""In-process pub/sub used to push progress to the browser over SSE."""

import json
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterator

_subscribers: set[queue.Queue] = set()
_lock = threading.Lock()

# Recent events so a page refresh can catch up instead of showing a blank log.
_history: deque[dict[str, Any]] = deque(maxlen=200)


def publish(kind: str, **payload: Any) -> dict[str, Any]:
    event = {
        "kind": kind,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **payload,
    }

    with _lock:
        _history.append(event)
        targets = list(_subscribers)

    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:
            # A browser tab that stopped reading; drop the event rather than
            # blocking the worker thread on it.
            pass

    return event


def log(message: str, level: str = "info") -> dict[str, Any]:
    return publish("log", message=message, level=level)


def history() -> list[dict[str, Any]]:
    with _lock:
        return list(_history)


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=500)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def stream() -> Iterator[str]:
    """Server-sent event stream. Heartbeats keep proxies from closing it."""
    q = subscribe()
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = q.get(timeout=15)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    finally:
        unsubscribe(q)
