#!/usr/bin/env python3
"""Bootstrap the first admin account for the Unify LLM user portal.

Usage:
    python scripts/create_admin.py --name alice --email alice@example.com
    python scripts/create_admin.py --name bob --email bob@lan --password 's3cret-pass'

Reads the password interactively when --password is omitted.
Uses UNIFY_USERS_DB (default data/unify_users.db). Does not touch the gateway process.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unify_llm.users import UserStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an admin portal account")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--email", required=True, help="Login email")
    parser.add_argument(
        "--password",
        default="",
        help="Password (prompted when omitted). Minimum 8 characters.",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Users SQLite path (default: UNIFY_USERS_DB or data/unify_users.db)",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password (min 8 chars): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("error: passwords do not match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 1

    db_path = Path(args.db) if args.db else None
    if db_path is None:
        env = os.environ.get("UNIFY_USERS_DB") or ""
        db_path = Path(env) if env else None

    store = UserStore(db_path)
    try:
        if store.get_user_by_email(args.email):
            print(f"error: email already exists: {args.email}", file=sys.stderr)
            print("Use the dashboard Users panel or PATCH /api/admin/users/{id} instead.", file=sys.stderr)
            return 1
        user = store.create_user(
            name=args.name,
            email=args.email,
            note="bootstrap admin (create_admin.py)",
            password=password,
            role="admin",
            status="active",
        )
    finally:
        store.close()

    print(f"created admin id={user['id']} name={user['name']} email={user['email']}")
    print("sign in at /portal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
