"""GET /events?app=desk|reporter: Server-Sent Events.

One poller per API process reads new rows from the events table every second and hands them
to every open stream, so 50 open browsers cost one query a second, not fifty.
The browser opens the stream with EventSource (withCredentials). On reconnect it sends
Last-Event-ID and we replay what it missed (the table keeps 24 hours).
"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.db import SessionLocal
from app.models import Event
from app.security import ANY_DESK_PAGE, load_caller

router = APIRouter(tags=["live updates"])
POLL_SECONDS = 1.0
PING_SECONDS = 25.0
RECHECK_SECONDS = 15.0  # how often an open stream re-checks the session and permissions


class Hub:
    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()
        self.last_id: int | None = None
        self.task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._poll())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def _poll(self) -> None:
        if self.last_id is None:
            self.last_id = await run_in_threadpool(_latest_id)
        while self.subscribers:
            try:
                rows = await run_in_threadpool(_fetch, None, self.last_id)
            except Exception:
                rows = []  # database blip: try again next tick
            for row in rows:
                self.last_id = row["id"]
                for queue in list(self.subscribers):
                    if not queue.full():
                        queue.put_nowait(row)
            await asyncio.sleep(POLL_SECONDS)


hub = Hub()


def _fetch(app: str | None, after: int) -> list[dict]:
    with SessionLocal() as db:
        q = select(Event).where(Event.id > after)
        if app:
            q = q.where(Event.app == app)
        return [{"id": e.id, "app": e.app, "type": e.type, "payload": e.payload}
                for e in db.scalars(q.order_by(Event.id).limit(500))]


def _latest_id() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.coalesce(func.max(Event.id), 0)))


@router.get("/events")
async def stream(request: Request, app: str = Query(..., pattern="^(desk|reporter)$")):
    # Own short session: a stream stays open for hours and must not hold a pooled connection.
    def auth():
        with SessionLocal() as db:
            return load_caller(db, request)

    def may_listen(c) -> bool:
        if app == "desk":
            return c.can("desk.view") and c.can_open(ANY_DESK_PAGE)
        return c.reporter is not None

    def still_allowed() -> bool:
        """Re-read the session and permissions: a disabled user, a deleted session or a removed permission ends
        the stream. A database blip does not."""
        try:
            return may_listen(auth())
        except HTTPException:
            return False
        except Exception:
            return True

    caller = await run_in_threadpool(auth)
    if app == "desk" and not may_listen(caller):
        raise HTTPException(403, "Your role cannot see the desk.")
    if app == "reporter" and not may_listen(caller):
        raise HTTPException(403, "This login is not linked to a reporter.")
    reporter_id = str(caller.reporter.id) if caller.reporter else None
    header = request.headers.get("Last-Event-ID", "")

    def visible(row: dict) -> bool:
        if row["app"] != app:
            return False
        return app != "reporter" or row["payload"].get("reporter_id") == reporter_id  # only their own stories

    def frame(row: dict) -> str:
        return f"id: {row['id']}\nevent: {row['type']}\ndata: {json.dumps(row['payload'], ensure_ascii=False)}\n\n"

    async def gen():
        queue = hub.subscribe()
        try:
            yield "retry: 3000\n\n"
            seen = 0
            if header.isdigit():  # replay what this browser missed
                for row in await run_in_threadpool(_fetch, app, int(header)):
                    seen = row["id"]
                    if visible(row):
                        yield frame(row)
            checked = pinged = time.monotonic()
            while not await request.is_disconnected():
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=min(PING_SECONDS, RECHECK_SECONDS))
                except asyncio.TimeoutError:
                    row = None
                t = time.monotonic()
                if t - checked >= RECHECK_SECONDS:
                    checked = t
                    if not await run_in_threadpool(still_allowed):
                        return  # signed out or no longer allowed: close; the browser's retry then gets 401/403
                if row is None:
                    if t - pinged >= PING_SECONDS:
                        pinged = t
                        yield ": ping\n\n"
                    continue
                if row["id"] > seen and visible(row):
                    yield frame(row)
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
