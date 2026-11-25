"""Local, metadata-only per-tool counts and latency histograms.

No request arguments, response contents, identity, or free-form exceptions are stored.
The SQLite database is separate from financial data; one row per day/tool/bucket/code.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, perf_counter

BUCKETS = (10, 25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000,
           5000, 10000, 20000, 30000, 60000, 120000, 300000, 600000, 86400000)
CODES = frozenset({"OK", "AUTH", "TIMEOUT", "RATE_LIMIT", "VALIDATION",
                   "UPSTREAM", "CANCELLED", "UNCLASSIFIED_ERROR", "HANDLER_EXCEPTION"})
RETENTION = 30
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def exception_code(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    if status in (401, 403) or name in ("AuthenticationError", "AuthorizationError"):
        return "AUTH"
    if status in (408, 504) or isinstance(exc, TimeoutError) or name.endswith("Timeout"):
        return "TIMEOUT"
    if status == 429:
        return "RATE_LIMIT"
    if status in (400, 422) or name in ("ValidationError", "ValueError"):
        return "VALIDATION"
    if type(status) is int and status >= 500:
        return "UPSTREAM"
    if name in ("CancelledError", "Cancelled"):
        return "CANCELLED"
    return "HANDLER_EXCEPTION"


def result_code(result: object) -> str:
    data = result if isinstance(result, Mapping) else getattr(result, "structured_content", None)
    # Older tools may return one JSON text block instead of structured_content.
    if data is None:
        content = getattr(result, "content", None)
        if isinstance(content, list) and len(content) == 1:
            text = getattr(content[0], "text", None)
            if isinstance(text, str) and len(text) <= 65536 and text.startswith("{"):
                with suppress(ValueError):
                    data = json.loads(text)
    error = (result.get("isError", False) if isinstance(result, Mapping)
             else getattr(result, "is_error", False))
    if isinstance(data, Mapping):
        error = error or data.get("success") is False
        failed_count = data.get("failed_count")
        error = error or (type(failed_count) is int and failed_count > 0)
        error = error or data.get("state") in ("error", "timed_out", "denied")
        error = error or (data.get("success") is not True and bool(data.get("error")))
        if error:
            if data.get("state") == "timed_out":
                return "TIMEOUT"
            if data.get("state") == "denied":
                return "AUTH"
            code = data.get("error_code")
            if isinstance(code, str) and code in CODES and code != "OK":
                return code
    return "UNCLASSIFIED_ERROR" if error else "OK"


class UsageStore:
    def __init__(self, path: Path, tools: frozenset[str], *, enabled: bool = True):
        self.path = path
        self.tools = tools - {"tool_usage"}
        self.enabled = enabled
        self._lock = Lock()
        self._pending: dict[tuple, list[int]] = {}
        self._count = 0
        self._last_flush = monotonic()
        self.recording_errors = 0
        self.dropped_calls = 0

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=0.05)
        conn.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT NOT NULL, tool TEXT NOT NULL, bucket INTEGER NOT NULL,
            code TEXT NOT NULL, calls INTEGER NOT NULL, sum_ms INTEGER NOT NULL,
            max_ms INTEGER NOT NULL, PRIMARY KEY(day,tool,bucket,code))""")
        return conn

    def _flush(self):
        if not self.enabled or not self._pending:
            return
        cutoff = (datetime.now(UTC).date() - timedelta(days=RETENTION-1)).isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
                conn.executemany(
                    """INSERT INTO usage VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(day,tool,bucket,code) DO UPDATE SET
                    calls=calls+excluded.calls, sum_ms=sum_ms+excluded.sum_ms,
                    max_ms=max(max_ms,excluded.max_ms)""",
                    [(*key, *value) for key, value in self._pending.items()],
                )
            self._pending.clear()
            self._count = 0
            self._last_flush = monotonic()
        except (OSError, sqlite3.Error):
            self.recording_errors += 1
            # Keep a bounded retry buffer rather than silently lose a whole batch.
            if self._count >= 512:
                self.dropped_calls += self._count
                self._pending.clear()
                self._count = 0

    def flush(self):
        with self._lock:
            self._flush()

    def record(self, name: str, elapsed_ms: float, code: str):
        if not self.enabled or name not in self.tools or os.getenv("PYTEST_CURRENT_TEST"):
            return
        if code not in CODES:
            code = "UNCLASSIFIED_ERROR"
        ms = max(0, min(86400000, round(elapsed_ms)))
        key = (utc_day(), name, bisect_left(BUCKETS, ms), code)
        with self._lock:
            entry = self._pending.setdefault(key, [0, 0, 0])
            entry[0] += 1
            entry[1] += ms
            entry[2] = max(entry[2], ms)
            self._count += 1
            if self._count >= 64 or monotonic() - self._last_flush >= 5:
                self._flush()

    def report(self, days: int = 7, limit: int = 50, tool: str | None = None) -> dict:
        if not 1 <= days <= RETENTION or not 1 <= limit <= 100:
            raise ValueError("Invalid report bounds")
        if tool is not None and not NAME.fullmatch(tool):
            raise ValueError("Invalid tool name")
        first = (datetime.now(UTC).date() - timedelta(days=days-1)).isoformat()
        today = utc_day()
        with self._lock:
            self._flush()
            rows = []
            if self.path.is_file():
                with closing(self._connect()) as conn, conn:
                    cutoff = (datetime.now(UTC).date()
                              - timedelta(days=RETENTION-1)).isoformat()
                    conn.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
                    rows = conn.execute(
                        "SELECT day,tool,bucket,code,calls,sum_ms,max_ms FROM usage "
                        "WHERE day BETWEEN ? AND ? AND (? IS NULL OR tool=?)",
                        (first, today, tool, tool),
                    ).fetchall()
            pending = self._count
            errors, dropped = self.recording_errors, self.dropped_calls
        totals = {}
        sampled_days = set()
        for day, name, bucket, code, n, elapsed, maximum in rows:
            if not NAME.fullmatch(name) or not 0 <= bucket < len(BUCKETS) or code not in CODES:
                continue
            sampled_days.add(day)
            a = totals.setdefault(name, {"calls": 0, "errors": 0, "sum_ms": 0,
                                         "max_ms": 0, "bins": defaultdict(int),
                                         "error_codes": defaultdict(int)})
            a["calls"] += n
            a["sum_ms"] += elapsed
            a["max_ms"] = max(a["max_ms"], maximum)
            a["bins"][bucket] += n
            if code != "OK":
                a["errors"] += n
                a["error_codes"][code] += n

        def upper(a, fraction):
            rank = a["calls"] * fraction
            seen = 0
            for bucket, n in sorted(a["bins"].items()):
                seen += n
                if seen >= rank:
                    return min(BUCKETS[bucket], a["max_ms"])
            return None

        ranked = []
        for name, a in totals.items():
            ranked.append({"tool": name, "calls": a["calls"], "errors": a["errors"],
                           "error_rate": round(a["errors"]/a["calls"], 4),
                           "mean_ms": round(a["sum_ms"]/a["calls"]),
                           "p50_upper_ms": upper(a, .50), "p95_upper_ms": upper(a, .95),
                           "max_ms": a["max_ms"], "error_codes": dict(a["error_codes"])})
        ranked.sort(key=lambda r: (-r["calls"], r["tool"]))
        inventory = {n for n in self.tools if tool is None or n == tool}
        return {"success": True, "enabled": self.enabled,
                "from_utc_day": first, "to_utc_day": today, "retention_days": RETENTION,
                "recorded_calls": sum(r["calls"] for r in ranked),
                "recorded_errors": sum(r["errors"] for r in ranked),
                "registered_tool_count": len(inventory),
                "unobserved_tools": sorted(inventory - totals.keys()),
                "tool_count": len(ranked), "returned_count": min(limit, len(ranked)),
                "truncated": len(ranked) > limit, "days_with_samples": sorted(sampled_days),
                "pending_calls": pending, "recording_errors_since_start": errors,
                "dropped_calls_since_start": dropped,
                "measurement_scope": "MCP call middleware; excludes client/network latency. "
                                     "Success does not certify data completeness.",
                "percentile_method": "Histogram bucket upper bounds; rounded milliseconds.",
                "tools": ranked[:limit]}


def create_store(service: str, tools: frozenset[str], env_prefix: str) -> UsageStore:
    root = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return UsageStore(root / service / "tool-usage.sqlite3", tools,
                      enabled=os.getenv(env_prefix + "_MCP_TOOL_USAGE") != "off")


async def observe_call(store, name, context, call_next):
    if not isinstance(name, str) or name not in store.tools:
        return await call_next(context)
    started = perf_counter()
    code = "HANDLER_EXCEPTION"
    try:
        result = await call_next(context)
        try:
            code = result_code(result)
        except Exception:
            code = "UNCLASSIFIED_ERROR"
        return result
    except BaseException as exc:
        with suppress(Exception):
            code = exception_code(exc)
        raise
    finally:
        # Observability must never change a financial or operational result.
        with suppress(Exception):
            store.record(name, (perf_counter()-started)*1000, code)


KNOWN_TOOLS = frozenset(["docs_append_markdown","docs_edit","docs_get","docs_insert_table","drive_append_text","drive_comment","drive_comments","drive_copy","drive_create","drive_get","drive_list","drive_move","drive_read","drive_recent","drive_rename","drive_search","drive_semantic_search","drive_share","drive_trash","drive_unshare","drive_untrash","drive_update_text","sheets_append","sheets_edit","sheets_get","sheets_read","sheets_write"])
_store = None


def configure_usage(path: Path) -> None:
    global _store
    _store = UsageStore(path, KNOWN_TOOLS, enabled=os.getenv("GDRIVE_MCP_TOOL_USAGE") != "off")
    atexit.register(_store.flush)


async def audit_tool_usage(ctx, call_next):
    if _store is None or ctx.method != "tools/call":
        return await call_next(ctx)
    params = ctx.params if isinstance(ctx.params, Mapping) else {}
    return await observe_call(_store, params.get("name"), ctx, call_next)


def usage_report(days=7, limit=50, tool=None):
    if _store is None:
        return {"success": False, "error": "Tool usage is not configured in this process"}
    try:
        return _store.report(days, limit, tool)
    except (OSError, sqlite3.Error, ValueError):
        return {"success": False, "error": "Tool usage report unavailable"}
