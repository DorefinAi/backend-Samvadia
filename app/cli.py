"""Admin commands.

    python -m app.cli create-user --username vansh --name "Vansh Arora" --role super_admin
    python -m app.cli import-reporters reporters.csv
    python -m app.cli reporter-login --phone 919876543210 --username sunita

The password is asked for on the terminal (or read from VAI_PASSWORD).
reporters.csv columns: name,slug_code,city_code,district,phone,email
"""
import argparse
import csv
import getpass
import os
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import ROLES, Reporter, User
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


def import_reporters(args) -> None:
    added = updated = 0
    with session_scope() as db, open(args.csv, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            phone = digits(row.get("phone")) or None
            email = (row.get("email") or "").strip().lower() or None
            existing = None
            if phone:
                existing = db.scalar(select(Reporter).where(Reporter.phone == phone))
            if existing is None and email:
                existing = db.scalar(select(Reporter).where(Reporter.email == email))
            values = {"name": row["name"].strip(), "slug_code": row["slug_code"].strip().upper(),
                      "city_code": row["city_code"].strip().upper(), "district": (row.get("district") or "").strip() or None,
                      "phone": phone, "email": email}
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                db.add(Reporter(org_id=get_settings().org_id, **values))
                added += 1
    print(f"Reporters added: {added}, updated: {updated}.")


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
    p.set_defaults(fn=import_reporters)
    p = sub.add_parser("reporter-login")
    p.add_argument("--phone", required=True)
    p.add_argument("--username", required=True)
    p.set_defaults(fn=reporter_login)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
