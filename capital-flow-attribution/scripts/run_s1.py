#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.pipeline import run_online, validate_fixture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the S1 daily capital-flow ledger or offline fixtures.")
    sub = parser.add_subparsers(dest="command", required=True)
    online = sub.add_parser("online")
    online.add_argument("--config", required=True, type=Path)
    online.add_argument("--run-id")
    fixture = sub.add_parser("fixture")
    fixture.add_argument("--fixture", required=True, type=Path)
    fixture.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "online":
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = run_online(config, run_id_override=args.run_id)
    else:
        result = validate_fixture(args.fixture, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("test_status", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
