"""Explicit local operator commands. Tokens print only on deliberate provisioning."""

import argparse
import secrets

from mateo.repository import SQLRepository
from mateo.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "provision-token"])
    parser.add_argument("--owner")
    parser.add_argument("--role", choices=["admin", "client"], default="client")
    args = parser.parse_args()
    repo = SQLRepository(Settings.from_environment().database_url)
    if args.command == "provision-token":
        if not args.owner:
            parser.error("--owner is required")
        token = secrets.token_urlsafe(32)
        with repo.transaction() as tx:
            tx.add_token(token, args.owner, args.role)
        print(token)
    else:
        print("Database schema initialized")
    repo.engine.dispose()


if __name__ == "__main__":
    main()
