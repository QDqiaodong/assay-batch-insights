"""Command-line entry point for the standalone longitudinal warehouse."""
import argparse
import json
import sys
from pathlib import Path

from .errors import WarehouseError
from .importer import import_manifest
from .query import build_query, render_query, write_query


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m assay_insights.warehouse",
        description=(
            "Capture synthetic assay batch reports into a read-mostly SQLite "
            "warehouse and rebuild historical batch decisions."
        ),
    )
    parser.add_argument("--db", required=True, type=Path,
                        help="SQLite warehouse path (created on import only)")
    parser.add_argument("--manifest", type=Path,
                        help="UTF-8 JSON import manifest (import mode)")
    parser.add_argument("--report", action="store_true",
                        help="report-only query mode; never writes the database")
    parser.add_argument("--knowledge-seq", type=int, default=None,
                        help="query a specific knowledge sequence (default: latest)")
    parser.add_argument("--batch", default=None,
                        help="optional exact batch filter")
    parser.add_argument("--output", type=Path, default=None,
                        help="write stable JSON here (default: print to stdout)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.report:
            return _run_query(args)
        if args.manifest is None:
            parser.error("either --manifest (import) or --report (query) is required")
        if args.knowledge_seq is not None or args.batch is not None:
            parser.error("--knowledge-seq/--batch require --report")
        return _run_import(args)
    except WarehouseError as exc:
        print(f"warehouse failed: {exc}", file=sys.stderr)
        return 2


def _run_import(args):
    result = import_manifest(args.db, args.manifest)
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_query(args):
    result = build_query(args.db, knowledge_seq=args.knowledge_seq, batch=args.batch)
    if args.output is None:
        text = render_query(result)
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    write_query(result, args.output)
    print(json.dumps({
        "knowledge_seq": result["knowledge_seq"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
