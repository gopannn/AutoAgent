from __future__ import annotations

import argparse

from .examples import build_supplier_demo
from .resolver import Resolver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctd", description="Constrained Topological Engine")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo", help="run the supplier-resolution demo")
    args = parser.parse_args(argv)

    if args.command == "demo":
        graph, query, policy = build_supplier_demo()
        result = Resolver(graph).resolve(query, policy)
        print(result.model_dump_json(indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
