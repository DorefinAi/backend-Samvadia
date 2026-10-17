"""Background worker: python -m app.worker  (Render Background Worker, same code as the API)."""
import logging

from app.config import get_settings
from app.db import session_scope
from app.jobs import enqueue, reset_stale, work_forever


def _queue(type_: str):
    def add() -> None:
        with session_scope() as db:
            enqueue(db, type_, {}, dedupe_key=type_)
    add.__name__ = f"queue_{type_}"
    return add


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    periodic = [(60, reset_stale), (600, _queue("purge_storage")), (3600, _queue("cleanup"))]
    if s.mail_enabled:
        periodic.append((s.mail_poll_seconds, _queue("fetch_mail")))
    logging.getLogger("worker").info("worker started; mail polling %s", "on" if s.mail_enabled else "off")
    work_forever(periodic)


if __name__ == "__main__":
    main()
