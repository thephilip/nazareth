"""Nazareth CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from nazareth.ledger.chain import Ledger
from nazareth.server import LEDGER_PATH, run_server


def cmd_serve(_args: argparse.Namespace) -> None:
    asyncio.run(run_server())


def cmd_verify(_args: argparse.Namespace) -> None:
    ledger = Ledger(LEDGER_PATH)
    valid, message = ledger.verify()
    print(message)
    sys.exit(0 if valid else 1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nazareth", description="Agent-native memory layer")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the MCP memory server (stdio)")
    sub.add_parser("verify", help="Verify the ledger hash chain integrity")

    args = parser.parse_args()
    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "verify":
        cmd_verify(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
