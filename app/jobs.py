"""A small Postgres job queue (the jobs table), shared by the API and the worker.

Why not a library: this is ~100 lines, needs no extra service, and works through
Supabase's session pooler. Retries: 10 s, 60 s, 5 min, then the job is `failed`
with the error kept on the row and the task's on_failure hook called.
"""
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import Job

log = logging.getLogger("jobs")
BACKOFF_SECONDS = [10, 60, 300]
STALE_AFTER = timedelta(minutes=30)


class RetryLater(Exception):
    """Not ready yet (e.g. files still downloading). Reschedules without using up an attempt."""

    def __init__(self, seconds: int = 15, reason: str = ""):
        super().__init__(reason)
        self.seconds = seconds


def now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, type_: str, payload: dict, *, delay: float = 0, dedupe_key: str | None = None,
            max_attempts: int = 4) -> None:
    """Add a job in the caller's transaction. With dedupe_key, a second pending copy is skipped."""
    stmt = insert(Job).values(
        type=type_, payload=payload, run_after=now() + timedelta(seconds=delay),
        dedupe_key=dedupe_key, max_attempts=max_attempts,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=text("status IN ('pending', 'running')"))
    db.execute(stmt)


def _claim() -> Job | None:
    with session_scope() as db:
        job_id = db.execute(text("""
            UPDATE jobs SET status = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'pending' AND run_after <= now()
                ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id
        """)).scalar()
        return db.get(Job, job_id) if job_id else None


def _finish(job_id: int, **values) -> None:
    with session_scope() as db:
        db.execute(update(Job).where(Job.id == job_id).values(**values))


def run_one() -> bool:
    """Run the next due job. Returns False when there was nothing to do."""
    from app.tasks import HANDLERS  # late import: tasks import this module

    job = _claim()
    if job is None:
        return False
    handler, on_failure = HANDLERS[job.type]
    try:
        with session_scope() as db:
            handler(db, job.payload)
        _finish(job.id, status="done", last_error=None)
    except RetryLater as e:
        _finish(job.id, status="pending", attempts=job.attempts - 1, last_error=str(e) or None,
                run_after=now() + timedelta(seconds=e.seconds))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("job %s %s attempt %s failed: %s", job.id, job.type, job.attempts, error)
        if job.attempts < job.max_attempts:
            wait = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
            _finish(job.id, status="pending", last_error=error, run_after=now() + timedelta(seconds=wait))
        else:
            _finish(job.id, status="failed", last_error=error + "\n" + traceback.format_exc()[-2000:])
            if on_failure:
                try:
                    with session_scope() as db:
                        on_failure(db, job.payload, error)
                except Exception:
                    log.exception("on_failure for job %s failed", job.id)
    return True


def reset_stale() -> None:
    """A worker that died mid-job leaves it 'running'; put it back in the queue."""
    with session_scope() as db:
        db.execute(update(Job).where(Job.status == "running", Job.locked_at < now() - STALE_AFTER)
                   .values(status="pending", run_after=now()))


def work_forever(periodic: list[tuple[float, callable]], idle_sleep: float = 1.0) -> None:
    """Worker main loop. periodic: [(every_seconds, fn)], each fn runs in the loop when due."""
    last_run = {fn: 0.0 for _, fn in periodic}
    while True:
        for every, fn in periodic:
            if time.monotonic() - last_run[fn] >= every:
                last_run[fn] = time.monotonic()
                try:
                    fn()
                except Exception:
                    log.exception("periodic %s failed", fn.__name__)
        try:
            busy = run_one()
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            time.sleep(idle_sleep)
