"""Admin commands for accounts. There's no public signup yet.

    python -m webapp.admin create-user you@example.com --plan pro
    python -m webapp.admin set-plan you@example.com pro
"""
from __future__ import annotations

import argparse

from .main import build_service
from .service import ServiceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("email")
    c.add_argument("--plan", default="free")
    p = sub.add_parser("set-plan")
    p.add_argument("email")
    p.add_argument("plan")
    args = parser.parse_args(argv)

    service, _ = build_service()
    try:
        if args.cmd == "create-user":
            key = service.create_user(args.email, plan=args.plan)
            print("API key (shown once, store it safely):")
            print(key)
        else:
            service.set_plan(args.email, args.plan)
            print(f"{args.email} is now on the {args.plan} plan")
    except ServiceError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
