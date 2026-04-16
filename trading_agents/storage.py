from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageLayout:
    root: Path
    market_data: Path
    sentiment_data: Path
    trade_logs: Path
    evaluation_logs: Path
    reports: Path
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


def build_storage_layout(root: str) -> StorageLayout:
    base = Path(root).expanduser()
    if not base.is_absolute():
        base = (Path.cwd() / base).resolve()

    layout = StorageLayout(
        root=base,
        market_data=base / "data" / "market",
        sentiment_data=base / "data" / "sentiment",
        trade_logs=base / "logs" / "trades",
        evaluation_logs=base / "logs" / "evaluations",
        reports=base / "reports",
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
    )
    for path in (
        layout.root,
        layout.market_data,
        layout.sentiment_data,
        layout.trade_logs,
        layout.evaluation_logs,
        layout.reports,
        layout.daily_reports,
        layout.service,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return layout
