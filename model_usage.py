"""Usage of observed model requests, including retries."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter


FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
          "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "request_seconds")
_collector = ContextVar("model_request_usage", default=None)


def combine(rows):
    rows = [row for row in rows if row and row.get("requests")]
    totals = {"requests": sum(row["requests"] for row in rows)}
    for field in FIELDS:
        values = [row.get(field) for row in rows]
        totals[field] = sum(values) if values and all(v is not None for v in values) else None
    return totals


@contextmanager
def collect_calls():
    rows = []
    token = _collector.set(rows)
    try:
        yield rows
    finally:
        _collector.reset(token)


@contextmanager
def observe_request():
    row = {"requests": 1}
    start = perf_counter()
    try:
        yield row
    finally:
        row["request_seconds"] = perf_counter() - start
        rows = _collector.get()
        if rows is not None:
            rows.append(row)
