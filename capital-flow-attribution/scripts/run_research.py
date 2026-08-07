#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.research_pipeline import run_research  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated G1 remediation and research-only historical backtest.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", help="Override the immutable run id without editing the frozen config.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.run_id:
        config["run_id"] = args.run_id
    print(json.dumps(run_research(config), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
