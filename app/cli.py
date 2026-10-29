"""Admin commands.

    python -m app.cli create-user --username vansh --name "Vansh Arora" --role super_admin
    python -m app.cli import-reporters reporters.csv [--dry-run]
    python -m app.cli reporter-login --phone 919876543210 --username sunita
    python -m app.cli backfill-items [--dry-run]     # reporter slug, district, kind for items from before build 1

The password is asked for on the terminal (or read from VAI_PASSWORD).
reporters.csv columns (header row, any order; only name and slug_code are required):
    name, slug_code, district_code, city_code, district, phone, emails, active
emails: one or more addresses separated by ; or spaces (the old single `email` column also works).
city_code defaults to district_code. A row matches an existing reporter by phone, else by any of its emails,
and updates it; emails are added to the ones the reporter already has. active: yes/no/true/false (default yes).
"""
import argparse
import csv
import getpass
import os
import re
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import ROLES, Location, Reporter, ReporterEmail, User
from app.security import hash_password
from app.services.whatsapp import digits


def _password() -> str:
    password = os.environ.get("VAI_PASSWORD") or getpass.getpass("Password (8+ characters): ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    return password


def create_user(args) -> None:
    if args.role not in ROLES:
        sys.exit(f"Role must be one of {', '.join(ROLES)}.")
    with session_scope() as db:
        if db.scalar(select(User.id).where(User.username == args.username.lower())):
            sys.exit("That username is taken.")
        db.add(User(org_id=get_settings().org_id, name=args.name, username=args.username.lower(),
                    password_hash=hash_password(_password()), role=args.role))
    print(f"Created {args.role} {args.username.lower()}.")


EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _row_emails(row: dict) -> list[str]:
    raw = " ".join(row.get(k) or "" for k in ("emails", "email"))
    out = []
    for e in re.split(r"[;,\s]+", raw.lower()):
        if e and EMAIL.match(e) and e not in out:
            out.append(e)
    return out


def _owner(db, email: str):
    listed = select(ReporterEmail.reporter_id).where(ReporterEmail.email == email)
    return db.scalar(select(Reporter).where((Reporter.email == email) | Reporter.id.in_(listed)).limit(1))


def import_reporters(args) -> None:
    """Add or update reporters from a CSV (see the module docstring). Prints one line per skipped row."""
    added = updated = skipped = 0
    with session_scope() as db, open(args.csv, encoding="utf-8-sig", newline="") as f:
        codes = set(db.scalars(select(Location.code)))
        for n, row in enumerate(csv.DictReader(f), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name, slug_code = row.get("name", ""), re.sub(r"[^A-Z0-9]", "", row.get("slug_code", "").upper())[:16]
            if not name or len(slug_code) < 2:
                print(f"line {n}: skipped, needs name and slug_code")
                skipped += 1
                continue
            district_code = row.get("district_code", "").upper() or None
            if district_code and district_code not in codes:
                print(f"line {n}: unknown district_code {district_code}, left empty")
                district_code = None
            city_code = row.get("city_code", "").upper() or district_code
            phone = digits(row.get("phone")) or None
            emails = _row_emails(row)
            existing = db.scalar(select(Reporter).where(Reporter.phone == phone)) if phone else None
            for email in emails:
                existing = existing or _owner(db, email)
            owners = {o.id for o in (_owner(db, e) for e in emails) if o is not None}
            if existing is not None:
                owners.discard(existing.id)
            if owners:
                print(f"line {n}: skipped, an email belongs to another reporter")
                skipped += 1
                continue
            active = row.get("active", "yes").lower() not in ("no", "false", "0", "n")
            reporter = existing or Reporter(org_id=get_settings().org_id, name=name, slug_code=slug_code)
            reporter.name, reporter.slug_code, reporter.active = name, slug_code, active
            reporter.district_code = district_code or reporter.district_code
            reporter.city_code = city_code or reporter.city_code
            reporter.district = row.get("district") or reporter.district
            reporter.phone = phone or reporter.phone
            if existing is None:
                db.add(reporter)
            all_emails = reporter.email_list + [e for e in emails if e not in reporter.email_list]
            have = {e.email for e in reporter.emails}
            for e in all_emails:
                if e not in have:
                    reporter.emails.append(ReporterEmail(email=e))
            for position, row_email in enumerate(reporter.emails):
                row_email.position = position
            reporter.email = reporter.email or (all_emails[0] if all_emails else None)
            db.flush()
            if existing is None:
                added += 1
            else:
                updated += 1
        if args.dry_run:
            db.rollback()
    print(f"Reporters added: {added}, updated: {updated}, skipped: {skipped}."
          + (" Dry run: nothing saved." if args.dry_run else ""))


def reporter_login(args) -> None:
    with session_scope() as db:
        reporter = db.scalar(select(Reporter).where(Reporter.phone == digits(args.phone)))
        if reporter is None:
            sys.exit("No reporter with that phone. Import the reporter list first.")
        if reporter.user_id:
            sys.exit("This reporter already has a login.")
        user = User(org_id=get_settings().org_id, name=reporter.name, username=args.username.lower(),
                    password_hash=hash_password(_password()), role="reporter")
        db.add(user)
        db.flush()
        reporter.user_id = user.id
    print(f"Reporter login {args.username.lower()} created for {reporter.name}.")


def backfill_items(args) -> None:
    """Reporter slug, district, kind and the footage search for items that came in before build 1.
    Safe to run again. A decided (approved or rejected) item keeps kind 'story', so nothing the desk already
    handled disappears from its default view; slugs, FTP folders and stories are never touched."""
    from app.intake import assign_district, first_line
    from app.models import Item, Message
    from app.services import districts as district_service
    from app.services import footage
    from app.services.reporter_slug import parse
    from app.services.slug import IST
    from app.services.triage import classify

    counts = {"items": 0, "slug": 0, "district": 0, "kind_changed": 0, "searching": 0}
    with session_scope() as db:
        districts = district_service.load(db)
        aliases, hindi = district_service.all_aliases(districts), district_service.hindi_names(districts)
        for item in db.scalars(select(Item).where(Item.reporter_slug.is_(None)).order_by(Item.received_at)):
            counts["items"] += 1
            messages = db.scalars(select(Message).where(Message.item_id == item.id).order_by(Message.received_at)).all()
            first = messages[0] if messages else None
            subject = (first.raw or {}).get("subject") if first else None
            text = first.text if first else item.body
            if item.source != "portal":
                for m in messages or [None]:
                    found = parse((m.raw or {}).get("subject") if m else None, first_line(m.text if m else item.body),
                                  received=item.received_at.astimezone(IST).date(), aliases=aliases, hindi=hindi)
                    if found:
                        item.reporter_slug, item.slug_key, item.parsed = found.reporter_slug, found.slug_key, found.stored()
                        counts["slug"] += 1
                        break
            before = item.district_code
            assign_district(db, item, districts)
            counts["district"] += item.district_code is not None and before != item.district_code
            if item.review_state == "pending":
                kind = classify(source=item.source, subject=subject, first_line=first_line(text or ""),
                                sender_address=item.sender_contact, sender_name=item.sender_name,
                                known_reporter=item.reporter_id is not None, has_slug=bool(item.reporter_slug))
                counts["kind_changed"] += kind != item.kind
                item.kind = kind
            footage.start(item)
            counts["searching"] += item.footage_status == "waiting"
        if args.dry_run:
            db.rollback()
    print(", ".join(f"{k}: {v}" for k, v in counts.items()) + (" (dry run: nothing saved)" if args.dry_run else ""))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("create-user")
    p.add_argument("--username", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", required=True)
    p.set_defaults(fn=create_user)
    p = sub.add_parser("import-reporters")
    p.add_argument("csv")
    p.add_argument("--dry-run", action="store_true", help="show what would change, save nothing")
    p.set_defaults(fn=import_reporters)
    p = sub.add_parser("backfill-items", help="reporter slug, district, kind for items from before build 1")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=backfill_items)
    p = sub.add_parser("reporter-login")
    p.add_argument("--phone", required=True)
    p.add_argument("--username", required=True)
    p.set_defaults(fn=reporter_login)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
