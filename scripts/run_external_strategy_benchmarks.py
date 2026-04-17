#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_agents.config import load_settings
from trading_agents.external_benchmarks import refresh_external_benchmark_suite
from trading_agents.storage import build_storage_layout


def main() -> int:
    parser = argparse.ArgumentParser(description="Run external strategy benchmarks for research-only candidates.")
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols to benchmark. Defaults to OBSERVATION_POOL.",
    )
    parser.add_argument("--force", action="store_true", help="Force refresh even if the benchmark TTL has not expired.")
    args = parser.parse_args()

    settings = load_settings()
    storage = build_storage_layout(settings.data_root)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] if args.symbols else list(settings.observation_pool)
    result = refresh_external_benchmark_suite(
        storage=storage,
        settings=settings,
        symbols=symbols,
        force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
