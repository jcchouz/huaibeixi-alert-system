#!/usr/bin/env python
"""Generate an Argon2 password hash for AUTH_USERS_JSON."""

from __future__ import annotations

import argparse
import json

from pwdlib import PasswordHash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("password", help="Plaintext password to hash")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--role",
        default="admin",
        choices=["viewer", "operator", "admin"],
    )
    args = parser.parse_args()

    password_hash = PasswordHash.recommended().hash(args.password)
    print(
        json.dumps(
            [
                {
                    "username": args.username,
                    "password_hash": password_hash,
                    "role": args.role,
                }
            ],
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
