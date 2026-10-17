"""Background worker (Render Background Worker, same code as the API).

    python -m app.worker                        all queues
    python -m app.worker --queues default,media
    python -m app.worker --queues gpu           later, on the GPU box
"""
import argparse
import logging
import signal

from app.config import get_settings
from app.db import session_scope
from app.jobs import ALL_QUEUES, Stop, enqueue, reset_stale, work_forever


def _queue(type_: str):
    def add() -> None:
        with session_scope() as db:
            enqueue(db, type_, {}, dedupe_key=type_)
    add.__name__ = f"queue_{type_}"
    return add


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.worker")
    parser.add_argument("--queues", default=",".join(ALL_QUEUES), help="comma-separated: default,media,gpu,cloud")
    parser.add_argument("--no-schedule", action="store_true", help="do not queue the periodic jobs (extra workers)")
    args = parser.parse_args()
    queues = [q.strip() for q in args.queues.split(",") if q.strip()]

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: setattr(Stop, "requested", True))
    s = get_settings()
    periodic = [(60, reset_stale)]
    if not args.no_schedule:
        periodic += [(600, _queue("purge_storage")), (3600, _queue("cleanup"))]
        if s.mail_enabled:
            periodic.append((s.mail_poll_seconds, _queue("fetch_mail")))
    log = logging.getLogger("worker")
    log.info("worker started; queues %s; mail polling %s", queues,
             "on" if s.mail_enabled and not args.no_schedule else "off")
    work_forever(periodic, queues)
    log.info("worker stopped cleanly")


if __name__ == "__main__":
    main()
