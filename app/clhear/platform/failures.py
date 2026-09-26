# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Safe, structured failure details for worker tasks and deployment reports.

A raw exception string from the database driver carries the SQL statement, its
bound parameters and sometimes source text — none of which may leave the worker.
This module reduces an exception to what an operator needs to act: the error
class, the SQLSTATE, a redacted one-line message, and the *first* cause kept
apart from the follow-on errors it produced (on PostgreSQL an aborted
transaction reports ``InFailedSqlTransaction`` for every later statement; the
statement that failed first is the one to fix).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

MAX_MESSAGE = 240
MAX_CHAIN = 6
MAX_SUMMARY = 25

# Anything that can carry protected text, credentials or bound values.
_SQL_BLOCK = re.compile(r"\[(?:SQL|parameters):.*", re.S)
# PostgreSQL repeats the failing statement in its own message ("LINE 1: SELECT …^").
_PG_LINE = re.compile(r"\bLINE \d+:.*?(?:\^|$)", re.S)
_CONTEXT = re.compile(r"\b(?:DETAIL|HINT|CONTEXT|QUERY|STATEMENT):.*", re.S)
_BACKGROUND = re.compile(r"\(Background on this error at:.*?\)", re.S)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"]+", re.I)
_DSN = re.compile(r"\b(postgres(?:ql)?(?:\+\w+)?|sqlite|mysql)://\S+", re.I)
_QUOTED = re.compile(r"(['\"])(?:(?!\1).){24,}\1", re.S)
_LONG_TOKEN = re.compile(r"\S{80,}")
_SECRET = re.compile(r"(?i)(password|secret|token|authorization|api[_-]?key)\s*[=:]\s*\S+")

FOLLOW_ON_CODES = frozenset({"InFailedSqlTransaction"})
FOLLOW_ON_SQLSTATES = frozenset({"25P02"})


def redact(text: str, limit: int = MAX_MESSAGE) -> str:
    """One line, no SQL, no parameters, no URLs, no long literals."""
    text = str(text or "")
    text = _SQL_BLOCK.sub("", text)
    text = _BACKGROUND.sub("", text)
    text = _PG_LINE.sub("", text)
    text = _CONTEXT.sub("", text)
    text = _SECRET.sub(r"\1=<redacted>", text)
    text = _DSN.sub("<dsn>", text)
    text = _URL.sub("<url>", text)
    text = _QUOTED.sub(r"\1<redacted>\1", text)
    text = _LONG_TOKEN.sub("<redacted>", text)
    text = " ".join(text.split())
    return text[:limit]


def _sqlstate(exc: BaseException) -> str | None:
    for candidate in (exc, getattr(exc, "orig", None)):
        if candidate is None:
            continue
        for attr in ("sqlstate", "pgcode"):
            value = getattr(candidate, attr, None)
            if isinstance(value, str) and value:
                return value
        diag = getattr(candidate, "diag", None)
        value = getattr(diag, "sqlstate", None) if diag is not None else None
        if isinstance(value, str) and value:
            return value
    return None


def _code(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return type(orig).__name__ if orig is not None else type(exc).__name__


def _chain(exc: BaseException) -> list[BaseException]:
    seen, out = set(), []
    node: BaseException | None = exc
    while node is not None and id(node) not in seen and len(out) < MAX_CHAIN:
        seen.add(id(node))
        out.append(node)
        node = node.__cause__ or (node.__context__ if not node.__suppress_context__ else None)
    return out


def describe(exc: BaseException, **context) -> dict:
    """Structured, exportable description of ``exc`` plus caller context
    (source, worker, task, stage, attempt, duration_ms …)."""
    chain = _chain(exc)
    entries = [{"error_type": type(e).__name__, "error_code": _code(e), "sqlstate": _sqlstate(e),
                "message": redact(e)} for e in chain]
    outer = entries[0]
    # The first cause is the deepest error in the chain that is not itself a
    # follow-on of an aborted transaction; failing that, the deepest error.
    primary = next((e for e in reversed(entries)
                    if e["error_code"] not in FOLLOW_ON_CODES and e["sqlstate"] not in FOLLOW_ON_SQLSTATES), entries[-1])
    follow_on = [e for e in entries if e is not primary and (e["error_code"] in FOLLOW_ON_CODES or e["sqlstate"] in FOLLOW_ON_SQLSTATES)]
    detail = {
        "error_type": outer["error_type"], "error_code": primary["error_code"], "sqlstate": primary["sqlstate"],
        "message": primary["message"], "first_cause": primary,
        "follow_on": follow_on, "aborted_transaction": bool(follow_on) or outer["error_code"] in FOLLOW_ON_CODES,
        "chain": entries, "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in context.items():
        if value is not None:
            detail[key] = value
    return detail


def task_failure_summary(tasks, *, worker: str, limit: int = MAX_SUMMARY) -> list[dict]:
    """Bounded, exportable summary of the tasks that did not complete or block.

    Codes only: even a redacted free-text message can quote a publisher's text
    or a URL fragment, so messages stay in the private ledger (``tasks.error``,
    ``summary.failure``) and never enter a deployment artifact."""
    rows = []
    for task in tasks:
        status = task["status"] if isinstance(task, dict) or hasattr(task, "__getitem__") else getattr(task, "status", None)
        if status in {"completed", "blocked"}:
            continue
        summary = (task.get("summary") if hasattr(task, "get") else None) or {}
        failure = summary.get("failure") or {}
        stages = summary.get("stages") or []
        started, finished = task.get("started_at"), task.get("finished_at")
        duration = None
        if started and finished:
            try:
                duration = int((finished - started).total_seconds() * 1000)
            except TypeError:
                duration = None
        rows.append({
            "source": task.get("source_key"), "worker": failure.get("worker") or worker, "task": task.get("task_id"),
            "status": status, "attempt": task.get("attempt"),
            "stage": failure.get("stage") or (stages[-1].get("stage") if stages and isinstance(stages[-1], dict) else None),
            "duration_ms": failure.get("duration_ms", duration),
            "error_type": failure.get("error_type"), "error_code": failure.get("error_code"),
            "sqlstate": failure.get("sqlstate"),
            "first_cause": (failure.get("first_cause") or {}).get("error_code"),
            "follow_on": [e.get("error_code") for e in failure.get("follow_on") or []],
            "aborted_transaction": failure.get("aborted_transaction"),
        })
        if len(rows) >= limit:
            break
    return rows
