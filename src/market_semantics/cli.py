"""Operator-invoked offline compilation and verification."""

import argparse
import json
import sys
from pathlib import Path

from market_semantics import __version__
from market_semantics.contracts import SemanticsError


def main() -> int:
    parser = argparse.ArgumentParser(prog="market-semantics")
    parser.add_argument("--version", action="version", version=__version__)
    operations = parser.add_subparsers(dest="operation", required=True)
    compile_parser = operations.add_parser("compile", help="compile two pinned metadata bundles")
    compile_parser.add_argument("--selection", type=Path, required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    verify_parser = operations.add_parser("verify", help="verify an immutable copied release")
    verify_parser.add_argument("--release", type=Path, required=True)
    verify_parser.add_argument("--expected-manifest-sha256", required=True)
    arguments = parser.parse_args()
    try:
        from .runtime import compile_release, verify_copied_release

        if arguments.operation == "compile":
            result = compile_release(arguments.selection, arguments.output)
        else:
            result = verify_copied_release(arguments.release, arguments.expected_manifest_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps({"code": "INTERRUPTED", "message": "operation interrupted"}), file=sys.stderr
        )
        return 130
    except (SemanticsError, OSError) as error:
        code = error.code if isinstance(error, SemanticsError) else "FILESYSTEM_ERROR"
        print(json.dumps({"code": code, "message": str(error)}), file=sys.stderr)
        return 124 if code == "TIMEOUT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
