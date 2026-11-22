"""Append-only task audit records, shared by concurrent model stages."""

import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

_scope = ContextVar("audit_scope", default=None)
_lock = threading.Lock()


@contextmanager
def trace_scope(path, phase):
    token = _scope.set((Path(path), phase) if path else None)
    try:
        yield
    finally:
        _scope.reset(token)


def emit(kind, payload, origin="system"):
    scope = _scope.get()
    if scope is None:
        return
    path, phase = scope
    event = {"ts": datetime.now(timezone.utc).isoformat(), "time": time.time(),
             "phase": phase, "kind": kind, "origin": origin, "payload": payload}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
