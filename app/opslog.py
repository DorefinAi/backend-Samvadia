"""Ops log for the Sentry control centre's Logs page (dev-docs/ops-logs-contract.md).

install(system, process, info) puts an OpsLogHandler on the root logger and starts two daemon threads:
- writer: every FLUSH_SECONDS, inserts the queued lines into ops_log on its own small engine (never a request session)
- heartbeat: every HEARTBEAT_SECONDS, upserts this process's row in ops_heartbeat

emit() only captures the record and queues it: it never touches the database, never raises and never logs through
`logging`. The queue keeps the newest MAX_QUEUE lines; older ones are dropped and counted (dropped(), served as
`dropped` by GET /owner/logs). Redaction (contract section 3) runs on the writer thread, before the insert.
No loops: records from sqlalchemy and this module, and anything logged on the writer / heartbeat threads (psycopg,
...), are skipped. The writer's own trouble goes to stderr, at most one line a minute.
"""
import atexit
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import date, datetime, timezone
from decimal import Decimal

MAX_QUEUE = 5000
FLUSH_SECONDS = 2.0
BATCH = 500
MAX_ATTEMPTS = 3  # a batch that fails this many times in a row is dropped (and counted)
HEARTBEAT_SECONDS = 30.0
MAX_MESSAGE = 2000
MAX_TRACE = 800  # with exc_info, at least this much of MAX_MESSAGE goes to the traceback's tail
CAPTURE = 4 * MAX_MESSAGE  # what emit() keeps of a long message / traceback before redaction
MAX_FIELDS = 20
MAX_FIELD_VALUE = 500
MAX_SHORT = 200  # logger, slug, item_id
STDERR_EVERY = 60.0
EXIT_WAIT = 5.0
# pg_advisory_xact_lock key taken by every ops_log insert: batches from the API and the workers commit one at a time,
# in id order, so a reader never sees id N+1 before N is committed and the `after` cursor never skips a line.
INSERT_LOCK = 730_100_008
SKIP_LOGGERS = ("sqlalchemy", __name__)
REDACTED = "[redacted]"

# --- redaction (contract section 3) --------------------------------------------------------------------------------

_SENSITIVE = r"pass(?:word)?|secret|token|api[_-]?key|authorization|cookie|session|otp|signature"
SENSITIVE_KEY = re.compile(_SENSITIVE, re.I)
_KEY = rf"(?<![\w$.-])([\w$.-]*(?:{_SENSITIVE})[\w.-]*)"  # a whole key that contains a sensitive word

_RULES: list[tuple[re.Pattern, object]] = [
    # URL credentials: scheme://user:pass@host
    (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@", re.I), rf"\1{REDACTED}@"),
    (re.compile(r"\b(Bearer)\s+[\w.~+/=-]+", re.I), rf"\1 {REDACTED}"),
    # "key": "value" / 'key': 'value' / "key": 123
    (re.compile(rf"""(["']){_KEY}\1(\s*:\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,}}\]]+)""", re.I),
     rf'\1\2\1\3"{REDACTED}"'),
    # key=value, also query parameters (?access_token=...&)
    (re.compile(rf"""{_KEY}(\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s&,;]+)""", re.I), rf"\1\2{REDACTED}"),
    # key: value, e.g. an Authorization header
    (re.compile(rf"""{_KEY}(:\s*)(?:(?:Bearer|Basic|Token|Digest)\s+)?[^\s,;'"]+""", re.I), rf"\1\2{REDACTED}"),
    # JWT-shaped: three base64url parts
    (re.compile(r"\b[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b"), REDACTED),
    # e-mail -> a***@domain
    (re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"), r"\1***@\2"),
    # phone, 10+ digits, optional + -> +91******1234 (not inside words, ids, paths or decimals)
    (re.compile(r"(?<![\w+./-])(\+?)(\d{10,})(?![\w-])"),
     lambda m: f"{m[1]}{m[2][:2]}{'*' * (len(m[2]) - 6)}{m[2][-4:]}"),
]


def redact_text(text: str) -> str:
    text = text.replace("\x00", "")  # Postgres text and jsonb refuse NUL
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def _scalar(value):
    """Keep JSON scalars; a UUID, date, time or Decimal becomes its string; anything else is left out (None)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) < 10**9 else str(value)  # 10+ digits may be a phone number: redacted as text
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (uuid.UUID, datetime, date, Decimal)):
        return str(value)
    return _SKIP


_SKIP = object()


def capture_fields(extras: dict) -> dict | None:
    """The scalar `extra=` values of a record, at most MAX_FIELDS, not yet redacted."""
    out = {}
    for key, value in extras.items():
        if len(out) >= MAX_FIELDS:
            break
        value = _scalar(value)
        if value is not _SKIP:
            out[str(key)[:64]] = value[:CAPTURE] if isinstance(value, str) else value
    return out or None


def redact_fields(fields: dict | None) -> dict | None:
    if not fields:
        return None
    out = {}
    for key, value in list(fields.items())[:MAX_FIELDS]:
        key = key.replace("\x00", "")
        if SENSITIVE_KEY.search(key):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = redact_text(value)[:MAX_FIELD_VALUE]
        else:
            out[key] = value
    return out


# --- records -> rows -----------------------------------------------------------------------------------------------

# Attributes every LogRecord has; anything else on a record came from extra= (uvicorn adds color_message)
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "taskName", "color_message"}


def level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    return "info" if levelno >= logging.INFO else "debug"


def _short(value) -> str | None:
    return None if value is None else str(value).replace("\x00", "")[:MAX_SHORT]


def capture(record: logging.LogRecord) -> dict:
    """What emit() keeps of a record (cheap; runs on the caller's thread). Redaction comes later (finish())."""
    try:
        message = record.getMessage()
    except Exception:  # bad %-args: keep the template rather than lose the line
        message = str(record.msg)
    trace = None
    if record.exc_info and record.exc_info[0] is not None:
        trace = record.exc_text or "".join(traceback.format_exception(*record.exc_info))
    elif record.exc_text:
        trace = record.exc_text
    extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
    return {"at": datetime.fromtimestamp(record.created, timezone.utc), "level": level_name(record.levelno),
            "logger": _short(record.name), "message": message[:CAPTURE],
            "trace": trace.rstrip()[-CAPTURE:] if trace else None,
            "slug": _short(extras.pop("slug", None)), "item_id": _short(extras.pop("item_id", None)),
            "fields": capture_fields(extras)}


def finish(raw: dict, system: str) -> dict:
    """A captured record as an ops_log row: redacted, message (and the traceback's tail) within MAX_MESSAGE."""
    message = redact_text(raw["message"])
    trace = redact_text(raw["trace"]) if raw.get("trace") else None
    if trace:
        head = message[:MAX_MESSAGE - MAX_TRACE - 1]
        room = MAX_MESSAGE - len(head) - 1
        message = head + "\n" + (trace if len(trace) <= room else "…" + trace[-(room - 1):])
    else:
        message = message[:MAX_MESSAGE]
    return {"at": raw["at"], "system": system, "level": raw["level"], "logger": raw["logger"], "message": message,
            "slug": raw["slug"], "item_id": raw["item_id"], "fields": redact_fields(raw["fields"])}


# --- plumbing -------------------------------------------------------------------------------------------------------

_local = threading.local()  # .internal = True on the writer / heartbeat threads (and during the exit flush)
_stop = threading.Event()
_engine_lock = threading.Lock()
_engine = None
_last_stderr: float | None = None


def _stderr(message: str) -> None:
    """At most one line a minute, straight to stderr (never through logging)."""
    global _last_stderr
    now = time.monotonic()
    if _last_stderr is not None and now - _last_stderr < STDERR_EVERY:
        return
    _last_stderr = now
    try:
        print(f"ops log: {message}", file=sys.__stderr__ or sys.stderr, flush=True)
    except Exception:
        pass


def _error(what: str, exc: Exception) -> None:
    _stderr(f"{what}: {type(exc).__name__}: {redact_text(str(exc))[:300]}")


def _db():
    """A small engine of its own (2 connections), so the log never competes with requests for the main pool."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from sqlalchemy import create_engine

            from app.config import get_settings

            _engine = create_engine(get_settings().sqlalchemy_url, pool_size=2, max_overflow=0, pool_pre_ping=True,
                                    pool_timeout=10, connect_args={"connect_timeout": 10})
        return _engine


def _skipped(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in SKIP_LOGGERS)


class OpsLogHandler(logging.Handler):
    def __init__(self, system: str, level: int = logging.INFO):
        super().__init__(level)
        self.system = system
        self.dropped = 0  # lines lost since start: queue overflow, or a batch the database refused MAX_ATTEMPTS times
        self._queue: deque[dict] = deque()
        self._queue_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: list[dict] | None = None  # a batch taken from the queue and not yet inserted
        self._attempts = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(_local, "internal", False) or _skipped(record.name):
                return
            raw = capture(record)
            with self._queue_lock:
                if len(self._queue) >= MAX_QUEUE:
                    self._queue.popleft()
                    self.dropped += 1
                self._queue.append(raw)
        except Exception as e:
            _error("could not queue a line", e)

    def queued(self) -> int:
        return len(self._queue)

    def take(self) -> list[dict]:
        """Up to BATCH queued lines, oldest first, as rows (redacted)."""
        with self._queue_lock:
            raw = [self._queue.popleft() for _ in range(min(len(self._queue), BATCH))]
        rows = []
        for r in raw:
            try:
                rows.append(finish(r, self.system))
            except Exception as e:
                with self._queue_lock:
                    self.dropped += 1
                _error("could not prepare a line", e)
        return rows

    def write_pending(self, wait: float = -1) -> None:
        """Insert everything queued, in batches. On a database error the batch is kept and tried again on the next
        call (the queue keeps filling, oldest dropped first); after MAX_ATTEMPTS failures it is dropped."""
        if not self._write_lock.acquire(timeout=wait):
            return
        try:
            while True:
                if self._pending is None:
                    rows = self.take()
                    if not rows:
                        return
                    self._pending, self._attempts = rows, 0
                try:
                    _insert(self._pending)
                except Exception as e:
                    self._attempts += 1
                    if self._attempts >= MAX_ATTEMPTS:
                        with self._queue_lock:
                            self.dropped += len(self._pending)
                        self._pending = None
                    _error(f"insert into ops_log failed (attempt {self._attempts})", e)
                    return
                self._pending = None
        finally:
            self._write_lock.release()


def _insert(rows: list[dict]) -> None:
    from sqlalchemy import insert, text

    from app.models import OpsLog

    with _db().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INSERT_LOCK})
        conn.execute(insert(OpsLog), rows)


def _beat(system: str, process: str, info: dict | None, started_at: datetime) -> None:
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert

    from app.models import OpsHeartbeat

    stmt = insert(OpsHeartbeat).values(process=process, system=system, host=socket.gethostname()[:MAX_SHORT],
                                       pid=os.getpid(), started_at=started_at, beat_at=func.now(), info=info)
    stmt = stmt.on_conflict_do_update(index_elements=["process"], set_={
        c: stmt.excluded[c] for c in ("system", "host", "pid", "started_at", "beat_at", "info")})
    with _db().begin() as conn:
        conn.execute(stmt)


def _writer_loop(handler: OpsLogHandler) -> None:
    _local.internal = True
    while not _stop.wait(FLUSH_SECONDS):
        try:
            handler.write_pending()
        except Exception as e:  # write_pending handles its own errors; this is the last guard for the thread
            _error("writer", e)


def _heartbeat_loop(system: str, process: str, info: dict | None, started_at: datetime) -> None:
    _local.internal = True
    while True:
        try:
            _beat(system, process, info, started_at)
        except Exception as e:
            _error("heartbeat failed", e)
        if _stop.wait(HEARTBEAT_SECONDS):
            return


def _at_exit(handler: OpsLogHandler) -> None:
    """Best effort: write what is still queued (waits at most EXIT_WAIT for a running batch)."""
    _local.internal = True
    try:
        handler.write_pending(wait=EXIT_WAIT)
    except Exception:
        pass
    _stop.set()


def _level(name: str) -> int:
    name = name.strip().upper()
    if name.isdigit():
        return int(name)
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def _version() -> str | None:
    try:
        from importlib.metadata import version

        return version("vartmaan-backend")
    except Exception:
        return None


def _keep_stderr_output(root: logging.Logger, level: int) -> None:
    """Adding a handler to root must not change what reaches stderr (Render's log):
    - root had no handler: Python's last resort printed WARNING and up; keep that with a real stderr handler
    - root's level drops to OPS_LOG_LEVEL so the ops log sees those lines; stderr handlers keep the old level"""
    old = root.level
    if not root.handlers:
        stderr = logging.StreamHandler()
        stderr.setLevel(logging.lastResort.level if logging.lastResort else logging.WARNING)
        stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(stderr)
    if level < old:
        for h in root.handlers:
            if h.level == logging.NOTSET:
                h.setLevel(old)
        root.setLevel(level)


_installed: OpsLogHandler | None = None
_install_lock = threading.Lock()


def install(system: str, process: str, info: dict | None = None) -> OpsLogHandler | None:
    """Start the ops log for this process. Idempotent: a second call (another lifespan, a reload) returns the same
    handler. OPS_LOG_ENABLED=false: does nothing and returns None."""
    global _installed
    with _install_lock:
        if _installed is not None:
            return _installed
        from app.config import get_settings

        settings = get_settings()
        if not settings.ops_log_enabled:
            return None
        handler = OpsLogHandler(system, _level(settings.ops_log_level))
        root = logging.getLogger()
        _keep_stderr_output(root, handler.level)
        root.addHandler(handler)
        # uvicorn's loggers do not propagate to root; "uvicorn" carries uvicorn.error (start-up, crashed requests)
        # but not uvicorn.access, which has its own propagate=False
        uv = logging.getLogger("uvicorn")
        if not uv.propagate:
            uv.addHandler(handler)
        facts = {"version": _version()}
        if os.environ.get("RENDER_GIT_COMMIT"):
            facts["commit"] = os.environ["RENDER_GIT_COMMIT"][:12]
        facts.update(info or {})
        started_at = datetime.now(timezone.utc)
        _stop.clear()
        threading.Thread(target=_writer_loop, args=(handler,), name="opslog-writer", daemon=True).start()
        threading.Thread(target=_heartbeat_loop, args=(system, process, facts, started_at), name="opslog-heartbeat",
                         daemon=True).start()
        atexit.register(_at_exit, handler)
        _installed = handler
        return handler


def flush(wait: float = EXIT_WAIT) -> None:
    """Write what is queued now (app shutdown). Never raises."""
    if _installed is not None:
        _local.internal = True
        try:
            _installed.write_pending(wait=wait)
        except Exception:
            pass
        finally:
            _local.internal = False


def dropped() -> int:
    """Lines this process dropped since it started (0 when the ops log is off)."""
    return _installed.dropped if _installed is not None else 0
