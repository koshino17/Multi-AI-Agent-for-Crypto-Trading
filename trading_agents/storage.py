from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageLayout:
    root: Path
    market_data: Path
    sentiment_data: Path
    alpha_arena_raw: Path
    alpha_arena_normalized: Path
    external_benchmark_normalized: Path
    trade_logs: Path
    evaluation_logs: Path
    reports: Path
    chart_reports: Path
    benchmark_reports: Path
    daily_reports: Path
    service: Path
    runner_supervisor_pid: Path
    runner_supervisor_log: Path
    runner_pid: Path
    runner_log: Path
    notion_sync_lock: Path
    notion_daily_review_state: Path
    strategy_memory_state: Path
    trade_cooldown_state: Path
    sentiment_http_cache_state: Path
    position_policy_state: Path
    equity_curve_history_state: Path
    equity_curve_svg: Path
    external_benchmark_state: Path


def mode_scoped_path(path: Path, mode: str) -> Path:
    safe_mode = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in str(mode).strip().lower())
    safe_mode = safe_mode.strip("-_") or "default"
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem}-{safe_mode}{suffix}")


def build_storage_layout(root: str) -> StorageLayout:
    base = Path(root).expanduser()
    if not base.is_absolute():
        base = (Path.cwd() / base).resolve()

    layout = StorageLayout(
        root=base,
        market_data=base / "data" / "market",
        sentiment_data=base / "data" / "sentiment",
        alpha_arena_raw=base / "data" / "alpha_arena" / "raw",
        alpha_arena_normalized=base / "data" / "alpha_arena" / "normalized",
        external_benchmark_normalized=base / "data" / "external_benchmarks" / "normalized",
        trade_logs=base / "logs" / "trades",
        evaluation_logs=base / "logs" / "evaluations",
        reports=base / "reports",
        chart_reports=base / "reports" / "charts",
        benchmark_reports=base / "reports" / "benchmarks",
        daily_reports=base / "reports" / "daily",
        service=base / "service",
        runner_supervisor_pid=base / "service" / "runner_supervisor.pid",
        runner_supervisor_log=base / "service" / "runner_supervisor.log",
        runner_pid=base / "service" / "runner.pid",
        runner_log=base / "service" / "runner.log",
        notion_sync_lock=base / "service" / "notion_sync.lock",
        notion_daily_review_state=base / "service" / "notion_daily_review.json",
        strategy_memory_state=base / "service" / "strategy_memory.json",
        trade_cooldown_state=base / "service" / "trade_cooldowns.json",
        sentiment_http_cache_state=base / "service" / "sentiment_http_cache.json",
        position_policy_state=base / "service" / "position_policy.json",
        equity_curve_history_state=base / "service" / "equity_curve_history.jsonl",
        equity_curve_svg=base / "reports" / "charts" / "equity-curve-latest.svg",
        external_benchmark_state=base / "service" / "external_benchmark_latest.json",
    )
    for path in (
        layout.root,
        layout.market_data,
        layout.sentiment_data,
        layout.alpha_arena_raw,
        layout.alpha_arena_normalized,
        layout.external_benchmark_normalized,
        layout.trade_logs,
        layout.evaluation_logs,
        layout.reports,
        layout.chart_reports,
        layout.benchmark_reports,
        layout.daily_reports,
        layout.service,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return layout
