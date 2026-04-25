from __future__ import annotations

import json
import os
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from uuid import uuid4

from trading_agents.reporting import LOCAL_TZ, _format_stage_latency_breakdown


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_FILE_UPLOAD_VERSION = "2026-03-11"


class NotionSyncClient:
    def __init__(self, token: str, page_id: str, page_title: str = "Trading Agents Live Status") -> None:
        self.token = token.strip()
        self.page_id = _normalize_page_id(page_id)
        self.page_title = page_title.strip() or "Trading Agents Live Status"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.page_id)

    def sync_status_page(self, report: dict[str, Any], daily_summary: dict[str, Any], runner_heartbeat: dict[str, str]) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "reason": "missing Notion token or status page id"}

        self._update_page_title()
        blocks = self._build_status_blocks(report, daily_summary, runner_heartbeat)
        self._replace_children(self.page_id, blocks)
        return {
            "status": "updated",
            "page_id": self.page_id,
            "blocks_written": len(blocks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def sync_heartbeat_page(self, daily_summary: dict[str, Any], runner_heartbeat: dict[str, str]) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "reason": "missing Notion token or status page id"}

        self._update_page_title()
        blocks = self._build_heartbeat_blocks(daily_summary, runner_heartbeat)
        self._replace_children(self.page_id, blocks)
        return {
            "status": "updated",
            "page_id": self.page_id,
            "blocks_written": len(blocks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "heartbeat",
        }

    def _build_status_blocks(
        self,
        report: dict[str, Any],
        daily_summary: dict[str, Any],
        runner_heartbeat: dict[str, str],
    ) -> list[dict[str, Any]]:
        latest = daily_summary.get("latest") or report
        idea = latest.get("idea", {})
        approval = latest.get("approval", {})
        backtest = latest.get("backtest", {})
        strategy_research = latest.get("strategy_research", {})
        debate = latest.get("debate", {})
        strategy_memory = latest.get("strategy_memory", {})
        account = latest.get("account", {})
        blocked_reasons = daily_summary.get("blocked_reason_counts", {})
        rejection_reasons = daily_summary.get("rejection_reason_counts", {})
        financial = daily_summary.get("financial_snapshot", {})
        equity_curve = daily_summary.get("equity_curve", {})
        top_blocked_reason = next(iter(blocked_reasons.items()), ("none", 0))
        top_rejected_reason = next(iter(rejection_reasons.items()), ("none", 0))
        stage_latency_seconds = daily_summary.get("stage_latency_seconds", {})
        stage_latency_p95_seconds = daily_summary.get("stage_latency_p95_seconds", {})
        long_proposals = int(daily_summary.get("long_proposals", 0))
        short_proposals = int(daily_summary.get("short_proposals", 0))
        long_accepted = int(daily_summary.get("long_accepted", 0))
        short_accepted = int(daily_summary.get("short_accepted", 0))
        external_benchmarks = daily_summary.get("external_benchmarks", {})
        top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
        top_alpha = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]

        blocks: list[dict[str, Any]] = [
            _heading_1(self.page_title),
            _paragraph(f"Last synced: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}"),
            _paragraph(f"Runner heartbeat: {runner_heartbeat.get('text', 'unavailable')}"),
            _heading_2("Live Status"),
            _bullet(f"Total Portfolio Value: {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT"),
            _bullet(
                f"Daily PnL: {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
                f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
            ),
            _bullet(
                f"Daily PnL Basis: {float(financial.get('day_start_portfolio_value_usdt', 0.0)):.2f} USDT "
                f"at {str(financial.get('day_start_timestamp_local', 'n/a')) or 'n/a'}"
            ),
            _bullet(
                "Realized PnL Split: "
                f"long={float(financial.get('realized_long_pnl_usdt', 0.0)):+.2f} USDT | "
                f"short={float(financial.get('realized_short_pnl_usdt', 0.0)):+.2f} USDT"
            ),
            _bullet(
                f"Available Balance: {float(financial.get('available_usdt', 0.0)):.2f} USDT "
                f"({float(financial.get('available_balance_ratio_pct', 100 - float(financial.get('capital_utilization_pct', 0.0)))):.1f}% of equity)"
            ),
            _bullet(
                f"Gross Exposure: {float(financial.get('gross_exposure_pct', financial.get('capital_utilization_pct', 0.0))):.1f}% of equity"
            ),
            _bullet(f"Effective Leverage: {float(financial.get('effective_leverage', 0.0)):.2f}x"),
            _bullet(
                "Directional Exposure: "
                f"long={float(financial.get('current_long_exposure_usdt', 0.0)):.2f} USDT | "
                f"short={float(financial.get('current_short_exposure_usdt', 0.0)):.2f} USDT"
            ),
            _bullet(
                f"Equity Curve: {equity_curve.get('sparkline', 'n/a')} "
                f"(range {float(equity_curve.get('min_value_usdt', 0.0)):.2f} - {float(equity_curve.get('max_value_usdt', 0.0)):.2f} USDT)"
            ),
            _bullet(f"Total decisions: {daily_summary.get('total', 0)}"),
            _bullet(f"Monitor heartbeats: {daily_summary.get('monitor_heartbeats', 0)}"),
            _bullet(f"Orders submitted: {daily_summary.get('submitted_orders', 0)}"),
            _bullet(f"Executed trades: {daily_summary.get('executed', 0)}"),
            _bullet(
                "Long vs Short: "
                f"proposals long={long_proposals}, short={short_proposals} | "
                f"accepted long={long_accepted}, short={short_accepted}"
            ),
            _bullet(f"Rejected orders: {daily_summary.get('rejected_orders', 0)}"),
            _bullet(f"Blocked proposals: {daily_summary.get('blocked', 0)}"),
            _bullet(f"Latency Breakdown Avg: {_format_stage_latency_breakdown(stage_latency_seconds, limit=5)}"),
            _bullet(f"Latency Breakdown P95: {_format_stage_latency_breakdown(stage_latency_p95_seconds, limit=5)}"),
            _bullet(
                "LLM Wake Rate: "
                f"{daily_summary.get('llm_wake_enabled', 0)}/{daily_summary.get('llm_wake_candidates', 0)} candidates "
                f"({float(daily_summary.get('llm_wake_rate_pct', 0.0)):.1f}%)"
            ),
            _bullet(f"Top Blocked Reason: {top_blocked_reason[0]} ({top_blocked_reason[1]})"),
            _bullet(f"Top Rejected Reason: {top_rejected_reason[0]} ({top_rejected_reason[1]})"),
        ]
        if top_benchmark.get("candidate_id"):
            blocks.extend(
                [
                    _heading_2("External Benchmark"),
                    _bullet(
                        f"Top benchmark: {top_benchmark.get('candidate_id')} on {top_benchmark.get('symbol', 'n/a')} "
                        f"(expectancy={float(top_benchmark.get('expectancy_pct', 0.0)):+.2f}% | "
                        f"profit_factor={float(top_benchmark.get('profit_factor', 0.0)):.2f})"
                    ),
                ]
            )
            if top_alpha.get("candidate_id"):
                blocks.append(
                    _bullet(
                        f"Top Alpha Arena model: {top_alpha.get('candidate_id')} on {top_alpha.get('symbol', 'n/a')} "
                        f"(expectancy={float(top_alpha.get('expectancy_pct', 0.0)):+.2f}% | "
                        f"profit_factor={float(top_alpha.get('profit_factor', 0.0)):.2f})"
                    )
                )

        blocks.extend(
            [
                _heading_2("Latest Decision"),
                _bullet(f"Selected Symbol: {latest.get('selected_symbol', 'n/a')}"),
                _bullet(f"Signal: {idea.get('action', 'n/a')} (score={float(idea.get('score', 0.0)):.2f})"),
                _bullet(f"Risk Decision: {approval.get('reason', 'n/a')}"),
            ]
        )

        if latest.get("selection_summary"):
            blocks.append(_bullet(f"Selection: {latest['selection_summary']}"))
        if debate.get("risk_feedback"):
            blocks.append(_bullet(f"Debate: risk raised {debate['risk_feedback']} before final decision"))
        if debate.get("memory_guard_reason"):
            blocks.append(_bullet(f"Memory Guard: {debate['memory_guard_reason']}"))
        controls = strategy_memory.get("controls") or {}
        if controls:
            blocks.append(_bullet(f"Learning Controls: {json.dumps(controls, ensure_ascii=False)}"))
        if account:
            if account.get("market_type") == "perp":
                account_line = (
                    "Account: "
                    f"equity {float(account.get('total_equity_usdt', account.get('free_usdt', 0.0))):.2f} USDT | "
                    f"available {float(account.get('available_balance_usdt', account.get('free_usdt', 0.0))):.2f} USDT | "
                    f"position {account.get('position_side', 'flat')} "
                    f"{float(account.get('base_asset', 0.0)):.6f} {account.get('base_symbol', '')} "
                    f"@ {float(account.get('entry_price', 0.0)):.4f} | "
                    f"UPnL {float(account.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT | "
                    f"Lev {float(account.get('leverage', 0.0)):.2f}x | "
                    f"Liq {float(account.get('liq_price', 0.0)):.4f} | "
                    f"Buffer {float(account.get('liquidation_buffer_pct', 0.0)):.2f}% | "
                    f"TP {float(account.get('take_profit_price', 0.0)):.4f} | "
                    f"SL {float(account.get('stop_loss_price', 0.0)):.4f}"
                )
            else:
                account_line = (
                    "Account: "
                    f"{float(account.get('free_usdt', 0.0)):.2f} USDT + "
                    f"{float(account.get('base_asset', 0.0)):.6f} {account.get('base_symbol', '')}".strip()
                )
                if account.get("dust_position"):
                    account_line += f" (dust ignored: {float(account.get('dust_notional_usdt', 0.0)):.2f} USDT)"
            blocks.append(
                _bullet(account_line)
            )

        result = latest.get("result")
        if result:
            blocks.append(_bullet(f"Execution: {result.get('status', 'unknown')}"))

        return blocks

    def _build_heartbeat_blocks(
        self,
        daily_summary: dict[str, Any],
        runner_heartbeat: dict[str, str],
    ) -> list[dict[str, Any]]:
        latest = daily_summary.get("latest") or {}
        idea = latest.get("idea", {})
        approval = latest.get("approval", {})
        blocked_reasons = daily_summary.get("blocked_reason_counts", {})
        financial = daily_summary.get("financial_snapshot", {})
        equity_curve = daily_summary.get("equity_curve", {})
        stage_latency_seconds = daily_summary.get("stage_latency_seconds", {})
        stage_latency_p95_seconds = daily_summary.get("stage_latency_p95_seconds", {})
        long_proposals = int(daily_summary.get("long_proposals", 0))
        short_proposals = int(daily_summary.get("short_proposals", 0))
        long_accepted = int(daily_summary.get("long_accepted", 0))
        short_accepted = int(daily_summary.get("short_accepted", 0))

        blocks: list[dict[str, Any]] = [
            _heading_1(self.page_title),
            _paragraph(f"Last synced: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}"),
            _paragraph(f"Runner heartbeat: {runner_heartbeat.get('text', 'unavailable')}"),
            _heading_2("Live Status"),
            _bullet(f"Total Portfolio Value: {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT"),
            _bullet(
                f"Daily PnL: {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
                f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
            ),
            _bullet(
                "Realized PnL Split: "
                f"long={float(financial.get('realized_long_pnl_usdt', 0.0)):+.2f} USDT | "
                f"short={float(financial.get('realized_short_pnl_usdt', 0.0)):+.2f} USDT"
            ),
            _bullet(
                "Directional Exposure: "
                f"long={float(financial.get('current_long_exposure_usdt', 0.0)):.2f} USDT | "
                f"short={float(financial.get('current_short_exposure_usdt', 0.0)):.2f} USDT"
            ),
            _bullet(
                f"Equity Curve: {equity_curve.get('sparkline', 'n/a')} "
                f"(range {float(equity_curve.get('min_value_usdt', 0.0)):.2f} - {float(equity_curve.get('max_value_usdt', 0.0)):.2f} USDT)"
            ),
            _bullet(
                f"Gross Exposure: {float(financial.get('gross_exposure_pct', financial.get('capital_utilization_pct', 0.0))):.1f}% of equity"
            ),
            _bullet(f"Effective Leverage: {float(financial.get('effective_leverage', 0.0)):.2f}x"),
            _bullet(f"Monitor heartbeats: {daily_summary.get('monitor_heartbeats', 0)}"),
            _bullet(f"Total decisions: {daily_summary.get('total', 0)}"),
            _bullet(f"Orders submitted: {daily_summary.get('submitted_orders', 0)}"),
            _bullet(f"Executed trades: {daily_summary.get('executed', 0)}"),
            _bullet(
                "Long vs Short: "
                f"proposals long={long_proposals}, short={short_proposals} | "
                f"accepted long={long_accepted}, short={short_accepted}"
            ),
            _bullet(f"Rejected orders: {daily_summary.get('rejected_orders', 0)}"),
            _bullet(f"Blocked proposals: {daily_summary.get('blocked', 0)}"),
            _bullet(f"Blocked by exchange minimum: {daily_summary.get('exchange_minimum_blocked', 0)}"),
            _bullet(f"Latency Breakdown Avg: {_format_stage_latency_breakdown(stage_latency_seconds, limit=5)}"),
            _bullet(f"Latency Breakdown P95: {_format_stage_latency_breakdown(stage_latency_p95_seconds, limit=5)}"),
            _bullet(
                "LLM Wake Rate: "
                f"{daily_summary.get('llm_wake_enabled', 0)}/{daily_summary.get('llm_wake_candidates', 0)} candidates "
                f"({float(daily_summary.get('llm_wake_rate_pct', 0.0)):.1f}%)"
            ),
        ]

        blocks.extend(
            [
                _heading_2("Latest Decision"),
                _bullet(f"Selected Symbol: {latest.get('selected_symbol', 'n/a')}"),
                _bullet(f"Signal: {idea.get('action', 'n/a')} (score={float(idea.get('score', 0.0)):.2f})"),
                _bullet(f"Risk Decision: {approval.get('reason', 'n/a')}"),
            ]
        )

        if blocked_reasons:
            blocks.append(_heading_2("Why Blocked"))
            for reason, count in blocked_reasons.items():
                blocks.append(_bullet(f"{reason}: {count}"))
        rejection_reasons = daily_summary.get("rejection_reason_counts", {})
        if rejection_reasons:
            blocks.append(_heading_2("Why Rejected"))
            for reason, count in rejection_reasons.items():
                blocks.append(_bullet(f"{reason}: {count}"))

        return blocks

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        notion_version: str = NOTION_VERSION,
    ) -> dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": notion_version,
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Notion API {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Notion API unavailable: {exc.reason}") from exc
        return json.loads(body) if body else {}

    def _multipart_request(self, url: str, file_path: Path, content_type: str) -> dict[str, Any]:
        boundary = f"----CodexNotion{uuid4().hex}"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = header + file_path.read_bytes() + footer
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_FILE_UPLOAD_VERSION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Notion file upload {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Notion file upload unavailable: {exc.reason}") from exc
        return json.loads(payload) if payload else {}

    def _prepare_equity_chart_upload(self, daily_summary: dict[str, Any]) -> str:
        equity_curve = daily_summary.get("equity_curve", {})
        chart_path_raw = str(equity_curve.get("chart_path", "")).strip()
        if not chart_path_raw:
            return ""
        chart_path = Path(chart_path_raw)
        if not chart_path.exists():
            return ""

        cache_path = chart_path.with_suffix(chart_path.suffix + ".notion-upload.json")
        digest = hashlib.sha256(chart_path.read_bytes()).hexdigest()
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
                if cache.get("sha256") == digest and cache.get("file_upload_id"):
                    return str(cache["file_upload_id"])
            except Exception:
                pass

        created = self._request(
            "POST",
            "/file_uploads",
            {
                "mode": "single_part",
                "filename": chart_path.name,
                "content_type": "image/svg+xml",
            },
            notion_version=NOTION_FILE_UPLOAD_VERSION,
        )
        upload_url = str(created.get("upload_url", "")).strip()
        upload_id = str(created.get("id", "")).strip()
        if not upload_url or not upload_id:
            raise RuntimeError("Notion file upload creation did not return upload_url/id")
        uploaded = self._multipart_request(upload_url, chart_path, "image/svg+xml")
        if str(uploaded.get("status", "")).lower() != "uploaded":
            raise RuntimeError(f"Notion file upload did not reach uploaded status: {uploaded}")

        cache_path.write_text(
            json.dumps(
                {
                    "sha256": digest,
                    "file_upload_id": upload_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return upload_id

    def _update_page_title(self) -> None:
        self._update_page_title_for(self.page_id, self.page_title)

    def _update_page_title_for(self, page_id: str, page_title: str) -> None:
        payload = {
            "properties": {
                "title": {
                    "title": [
                        {
                            "type": "text",
                            "text": {"content": page_title[:2000]},
                        }
                    ]
                }
            }
        }
        self._request("PATCH", f"/pages/{page_id}", payload)

    def _list_block_children(self, block_id: str) -> list[str]:
        block_ids: list[str] = []
        next_cursor = ""
        while True:
            query = f"?page_size=100{f'&start_cursor={parse.quote(next_cursor)}' if next_cursor else ''}"
            payload = self._request("GET", f"/blocks/{block_id}/children{query}")
            for item in payload.get("results", []):
                item_id = item.get("id")
                if item_id:
                    block_ids.append(str(item_id))
            if not payload.get("has_more"):
                break
            next_cursor = str(payload.get("next_cursor", ""))
            if not next_cursor:
                break
        return block_ids

    def _archive_block(self, block_id: str) -> None:
        try:
            self._request("PATCH", f"/blocks/{block_id}", {"archived": True})
        except RuntimeError as exc:
            if "Can't edit block that is archived" not in str(exc):
                raise

    def _append_children(self, block_id: str, blocks: list[dict[str, Any]]) -> None:
        notion_version = NOTION_VERSION
        if any(
            block.get("type") == "image"
            and block.get("image", {}).get("type") == "file_upload"
            for block in blocks
        ):
            notion_version = NOTION_FILE_UPLOAD_VERSION
        for index in range(0, len(blocks), 100):
            self._request(
                "PATCH",
                f"/blocks/{block_id}/children",
                {"children": blocks[index:index + 100]},
                notion_version=notion_version,
            )

    def _replace_children(self, block_id: str, blocks: list[dict[str, Any]]) -> None:
        existing_children = self._list_block_children(block_id)
        for child_id in existing_children:
            self._archive_block(child_id)
        # Notion block archival can lag slightly; wait for the page to actually
        # look empty before appending a fresh status snapshot to avoid duplicates.
        for _ in range(20):
            if not self._list_block_children(block_id):
                break
            time.sleep(0.25)
        else:
            for child_id in self._list_block_children(block_id):
                self._archive_block(child_id)
            time.sleep(0.5)
        self._append_children(block_id, blocks)

    def create_child_page(self, parent_page_id: str, page_title: str, blocks: list[dict[str, Any]]) -> str:
        normalized_parent = _normalize_page_id(parent_page_id)
        payload = {
            "parent": {"type": "page_id", "page_id": normalized_parent},
            "properties": {
                "title": {
                    "title": [
                        {
                            "type": "text",
                            "text": {"content": page_title[:2000]},
                        }
                    ]
                }
            },
            "children": blocks[:100],
        }
        notion_version = NOTION_VERSION
        if any(
            block.get("type") == "image"
            and block.get("image", {}).get("type") == "file_upload"
            for block in blocks[:100]
        ):
            notion_version = NOTION_FILE_UPLOAD_VERSION
        created = self._request("POST", "/pages", payload, notion_version=notion_version)
        page_id = str(created.get("id", ""))
        if not page_id:
            raise RuntimeError("Notion API did not return a page id for the daily review page")
        if len(blocks) > 100:
            self._append_children(page_id, blocks[100:])
        return page_id

    def replace_page_content(self, page_id: str, page_title: str, blocks: list[dict[str, Any]]) -> None:
        self._update_page_title_for(page_id, page_title)
        self._replace_children(page_id, blocks)


def _acquire_lock(lock_path: str | Path) -> int | None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age_seconds = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
            if age_seconds > 180:
                path.unlink()
                return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            pass
        return None


def _release_lock(lock_path: str | Path, fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        Path(lock_path).unlink()
    except OSError:
        pass


def sync_notion_status(
    token: str,
    page_id: str,
    page_title: str,
    report: dict[str, Any],
    daily_summary: dict[str, Any],
    runner_heartbeat: dict[str, str],
    lock_path: str | Path,
) -> dict[str, Any]:
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        return {"status": "busy", "reason": "another Notion sync is already running"}
    client = NotionSyncClient(token=token, page_id=page_id, page_title=page_title)
    try:
        return client.sync_status_page(report, daily_summary, runner_heartbeat)
    finally:
        _release_lock(lock_path, lock_fd)


def sync_notion_heartbeat(
    token: str,
    page_id: str,
    page_title: str,
    daily_summary: dict[str, Any],
    runner_heartbeat: dict[str, str],
    lock_path: str | Path,
) -> dict[str, Any]:
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        return {"status": "busy", "reason": "another Notion sync is already running"}
    client = NotionSyncClient(token=token, page_id=page_id, page_title=page_title)
    try:
        return client.sync_heartbeat_page(daily_summary, runner_heartbeat)
    finally:
        _release_lock(lock_path, lock_fd)


def sync_notion_daily_review(
    token: str,
    parent_page_id: str,
    date_label: str,
    page_title_prefix: str,
    daily_review: dict[str, Any],
    daily_summary: dict[str, Any],
    state_path: str | Path,
    lock_path: str | Path,
) -> dict[str, Any]:
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        return {"status": "busy", "reason": "another Notion sync is already running"}
    client = NotionSyncClient(token=token, page_id="")
    title = f"{page_title_prefix.strip() or 'Trading Agents Daily Review'} - {date_label}"
    equity_chart_upload_id = client._prepare_equity_chart_upload(daily_summary)
    blocks = _build_daily_review_blocks(title, date_label, daily_review, daily_summary, equity_chart_upload_id)
    state_file = Path(state_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    page_id = ""
    try:
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                if state.get("date_label") == date_label:
                    page_id = _normalize_page_id(str(state.get("page_id", "")))
                    if page_id:
                        return {
                            "status": "skipped",
                            "reason": "daily review already published for this Taiwan date",
                            "page_id": page_id,
                            "mode": "daily_review",
                        }
            except Exception:
                page_id = ""
        if page_id:
            client.replace_page_content(page_id, title, blocks)
            status = "updated"
        else:
            page_id = client.create_child_page(parent_page_id, title, blocks)
            status = "created"
        state_file.write_text(json.dumps({"date_label": date_label, "page_id": page_id}, ensure_ascii=False, indent=2))
        return {
            "status": status,
            "page_id": page_id,
            "blocks_written": len(blocks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "daily_review",
        }
    finally:
        _release_lock(lock_path, lock_fd)


def _normalize_page_id(value: str) -> str:
    cleaned = value.strip().split("?")[0].rstrip("/")
    compact = cleaned.rsplit("/", 1)[-1].replace("-", "")
    if len(compact) == 32:
        return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"
    return cleaned


def _rich_text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _paragraph(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(content)}}


def _bullet(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(content)},
    }


def _heading_1(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": _rich_text(content)}}


def _heading_2(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(content)}}


def _image_file_upload(file_upload_id: str, caption: str = "") -> dict[str, Any]:
    block: dict[str, Any] = {
        "object": "block",
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": file_upload_id},
            "caption": _rich_text(caption) if caption else [],
        },
    }
    return block


def _build_daily_review_blocks(
    title: str,
    date_label: str,
    daily_review: dict[str, Any],
    daily_summary: dict[str, Any],
    equity_chart_upload_id: str = "",
) -> list[dict[str, Any]]:
    blocked_reasons = daily_summary.get("blocked_reason_counts", {})
    rejection_reasons = daily_summary.get("rejection_reason_counts", {})
    latest = daily_summary.get("latest") or {}
    debate = latest.get("debate", {})
    strategy_memory = latest.get("strategy_memory", {})
    financial = daily_summary.get("financial_snapshot", {})
    equity_curve = daily_summary.get("equity_curve", {})
    avg_scores = daily_summary.get("avg_scores", {})
    stage_latency_seconds = daily_summary.get("stage_latency_seconds", {})
    stage_latency_p95_seconds = daily_summary.get("stage_latency_p95_seconds", {})
    external_benchmarks = daily_summary.get("external_benchmarks", {})
    top_benchmark = (external_benchmarks.get("top_candidates") or [{}])[0]
    top_alpha = (external_benchmarks.get("top_alpha_arena_candidates") or [{}])[0]
    symbol_postmortem = daily_summary.get("symbol_postmortem") or {}
    trade_review = daily_summary.get("trade_review") or {}
    loss_attribution = daily_summary.get("loss_attribution") or {}
    policy_exit_diagnostics = daily_summary.get("policy_exit_diagnostics") or {}
    external_ai_review = daily_summary.get("external_ai_review") or {}
    blocks: list[dict[str, Any]] = [
        _heading_1(title),
        _paragraph(f"Published at: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}"),
        _heading_2("Financial Snapshot"),
        _bullet(
            f"Total Portfolio Value: {float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT "
            f"(Configured Initial: {float(financial.get('initial_capital_usdt', 0.0)):.2f} USDT)"
        ),
        _bullet(
            f"Daily PnL: {float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
            f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
        ),
        _bullet(
            f"Daily PnL Basis: {float(financial.get('day_start_portfolio_value_usdt', 0.0)):.2f} USDT "
            f"at {str(financial.get('day_start_timestamp_local', 'n/a')) or 'n/a'}"
        ),
        _bullet(
            f"PnL Bridge: realized {float(financial.get('realized_pnl_usdt', 0.0)):+.2f} "
            f"+ unrealized change {float(financial.get('unrealized_change_usdt', 0.0)):+.2f} "
            f"+ residual {float(financial.get('pnl_bridge_residual_usdt', 0.0)):+.2f}"
        ),
        _bullet(f"Realized PnL: {float(financial.get('realized_pnl_usdt', 0.0)):+.2f} USDT"),
        _bullet(f"Unrealized PnL: {float(financial.get('unrealized_pnl_usdt', 0.0)):+.2f} USDT"),
        _bullet(f"Daily Fees Paid: {float(financial.get('daily_fees_usdt', 0.0)):.2f} USDT"),
        _bullet(f"Cumulative Fees Paid: {float(financial.get('cumulative_fees_usdt', 0.0)):.2f} USDT"),
        _bullet(
            f"Equity Curve: {equity_curve.get('sparkline', 'n/a')} "
            f"(range {float(equity_curve.get('min_value_usdt', 0.0)):.2f} - {float(equity_curve.get('max_value_usdt', 0.0)):.2f} USDT)"
        ),
        _heading_2("Current Portfolio"),
        _bullet(
            f"Available USDT: {float(financial.get('available_usdt', 0.0)):.2f} USDT "
            f"({100 - float(financial.get('capital_utilization_pct', 0.0)):.1f}%)"
        ),
        _bullet(f"Capital Utilization: {float(financial.get('capital_utilization_pct', 0.0)):.1f}%"),
        _heading_2("Daily Snapshot"),
        _bullet(f"Date: {date_label}"),
        _bullet(f"Total decisions: {daily_summary.get('total', 0)}"),
        _bullet(f"Trade proposals: {daily_summary.get('proposals', 0)}"),
        _bullet(f"Approved by risk: {daily_summary.get('approved', 0)}"),
        _bullet(f"Orders submitted: {daily_summary.get('submitted_orders', 0)}"),
        _bullet(f"Executed trades: {daily_summary.get('executed', 0)}"),
        _bullet(f"Rejected orders: {daily_summary.get('rejected_orders', 0)}"),
        _bullet(f"Avg Decision Latency: {float(daily_summary.get('avg_decision_latency_seconds', 0.0)):.2f} seconds"),
        _bullet(f"Latency Breakdown Avg: {_format_stage_latency_breakdown(stage_latency_seconds)}"),
        _bullet(f"Latency Breakdown P95: {_format_stage_latency_breakdown(stage_latency_p95_seconds)}"),
        _bullet(
            "LLM Wake Rate: "
            f"{daily_summary.get('llm_wake_enabled', 0)}/{daily_summary.get('llm_wake_candidates', 0)} candidates "
            f"({float(daily_summary.get('llm_wake_rate_pct', 0.0)):.1f}%)"
        ),
        _bullet(
            f"Agent Confidence Distribution: buy={float(avg_scores.get('buy', 0.0)):.2f} | "
            f"sell={float(avg_scores.get('sell', 0.0)):.2f} | hold={float(avg_scores.get('hold', 0.0)):.2f}"
        ),
        _heading_2("Operations Summary"),
        _paragraph(str(daily_review.get("operations_summary", ""))),
        _heading_2("Decision Summary"),
        _paragraph(str(daily_review.get("decision_summary", ""))),
    ]
    strategy_sections = [
        ("Strategist View", str(daily_review.get("strategist_review", "")).strip()),
        ("Risk View", str(daily_review.get("risk_review", "")).strip()),
        ("Benchmark View", str(daily_review.get("benchmark_review", "")).strip()),
        ("Execution View", str(daily_review.get("execution_review", "")).strip()),
    ]
    strategy_sections = [(title, body) for title, body in strategy_sections if body]
    if strategy_sections or str(daily_review.get("consensus_summary", "")).strip() or list(daily_review.get("action_items", []) or []):
        blocks.append(_heading_2("Strategy Review"))
        for title, body in strategy_sections:
            blocks.append(_bullet(f"{title}: {body}"))
        consensus = str(daily_review.get("consensus_summary", "")).strip()
        if consensus:
            blocks.append(_bullet(f"Consensus: {consensus}"))
        for item in list(daily_review.get("action_items", []) or [])[:5]:
            blocks.append(_bullet(f"Action Item: {item}"))
    if external_ai_review and external_ai_review.get("status") not in {"disabled", ""}:
        blocks.append(_heading_2("External AI Review"))
        if external_ai_review.get("provider") or external_ai_review.get("model"):
            blocks.append(
                _bullet(
                    f"Reviewer: {external_ai_review.get('provider', 'n/a')} / "
                    f"{external_ai_review.get('model', 'n/a')}"
                )
            )
        if external_ai_review.get("summary"):
            blocks.append(_bullet(f"Summary: {external_ai_review.get('summary')}"))
        if external_ai_review.get("verdict"):
            blocks.append(_bullet(f"Verdict: {external_ai_review.get('verdict')}"))
        for item in external_ai_review.get("strengths", [])[:4]:
            blocks.append(_bullet(f"Strength: {item}"))
        for item in external_ai_review.get("concerns", [])[:4]:
            blocks.append(_bullet(f"Concern: {item}"))
        for item in external_ai_review.get("action_items", [])[:4]:
            blocks.append(_bullet(f"External Action Item: {item}"))
    if equity_chart_upload_id:
        blocks.extend(
            [
                _heading_2("Equity Chart"),
                _image_file_upload(equity_chart_upload_id, caption="Daily equity curve"),
            ]
        )
    if top_benchmark.get("candidate_id"):
        blocks.extend(
            [
                _heading_2("External Benchmarks"),
                _bullet(f"Refreshed at: {external_benchmarks.get('generated_at', 'n/a')}"),
                _bullet(f"Live baseline strategy: {external_benchmarks.get('baseline_strategy_id', 'n/a')}"),
                _bullet(
                    f"Top benchmark: {top_benchmark.get('candidate_id')} on {top_benchmark.get('symbol', 'n/a')} "
                    f"(expectancy={float(top_benchmark.get('expectancy_pct', 0.0)):+.2f}% | "
                    f"profit_factor={float(top_benchmark.get('profit_factor', 0.0)):.2f} | "
                    f"trades={int(top_benchmark.get('trade_count', 0))})"
                ),
            ]
        )
        if top_alpha.get("candidate_id"):
            blocks.append(
                _bullet(
                    f"Top Alpha Arena model: {top_alpha.get('candidate_id')} on {top_alpha.get('symbol', 'n/a')} "
                    f"(expectancy={float(top_alpha.get('expectancy_pct', 0.0)):+.2f}% | "
                    f"profit_factor={float(top_alpha.get('profit_factor', 0.0)):.2f})"
            )
        )
    if symbol_postmortem:
        blocks.extend(
            [
                _heading_2("Symbol Postmortem"),
                _bullet(f"Focus Symbol: {symbol_postmortem.get('symbol', 'n/a')}"),
                _paragraph(str(symbol_postmortem.get("summary", ""))),
            ]
        )
        for item in symbol_postmortem.get("improvement_directions", [])[:4]:
            blocks.append(_bullet(f"Improvement: {item}"))
    if trade_review:
        blocks.extend(
            [
                _heading_2("Trade Review"),
                _bullet(
                    "Position Episodes: "
                    f"long={int(trade_review.get('long_episodes', 0))} | "
                    f"short={int(trade_review.get('short_episodes', 0))} | "
                    f"wins={int(trade_review.get('closed_winners', 0))} | "
                    f"losses={int(trade_review.get('closed_losers', 0))} | "
                    f"open={int(trade_review.get('open_episodes', 0))}"
                ),
            ]
        )
        for episode in (trade_review.get("episodes") or [])[:8]:
            symbol = str(episode.get("symbol", "n/a"))
            direction = str(episode.get("direction", "n/a"))
            opened_at = str(episode.get("opened_at", "n/a"))
            entries = int(episode.get("entries", 0))
            avg_entry = float(episode.get("avg_entry_price", 0.0))
            close_or_mark = float(episode.get("close_or_mark_price", 0.0))
            estimated_edge_pct = float(episode.get("estimated_edge_pct", 0.0))
            status = str(episode.get("status", "unknown"))
            entry_source = str(episode.get("entry_source", "unknown"))
            blocks.append(
                _bullet(
                    f"{symbol} {direction} | opened {opened_at} | entries={entries} | "
                    f"avg_entry={avg_entry:.4f} | close/mark={close_or_mark:.4f} | "
                    f"edge={estimated_edge_pct:+.2f}% | status={status} | source={entry_source}"
                )
            )
            close_reason = str(episode.get("close_reason", "")).strip()
            if close_reason:
                blocks.append(_bullet(f"Close reason: {close_reason}"))
    if policy_exit_diagnostics:
        blocks.extend(
            [
                _heading_2("Policy Exit Diagnostics"),
                _bullet(str(policy_exit_diagnostics.get("summary", "n/a"))),
            ]
        )
    if loss_attribution:
        blocks.extend(
            [
                _heading_2("Loss Attribution"),
                _bullet(f"Primary Driver: {loss_attribution.get('primary_driver', 'n/a')}"),
                _bullet(
                    f"Realized After Fees: {float(loss_attribution.get('realized_after_fees_usdt', 0.0)):+.2f} USDT"
                ),
            ]
        )
        accepted = loss_attribution.get("accepted_source_counts") or {}
        if accepted:
            blocks.append(
                _bullet("Accepted by Source: " + " | ".join(f"{k}={int(v)}" for k, v in accepted.items()))
            )
        losing_sources = loss_attribution.get("losing_episode_source_counts") or {}
        if losing_sources:
            blocks.append(
                _bullet(
                    "Losing Episodes by Source: "
                    + " | ".join(f"{k}={int(v)}" for k, v in losing_sources.items())
                )
            )
        losing_dirs = loss_attribution.get("losing_episode_direction_counts") or {}
        if losing_dirs:
            blocks.append(
                _bullet(
                    "Losing Episodes by Direction: "
                    + " | ".join(f"{k}={int(v)}" for k, v in losing_dirs.items())
                )
            )
        avg_loss_source = loss_attribution.get("avg_loss_edge_by_source_pct") or {}
        if avg_loss_source:
            blocks.append(
                _bullet(
                    "Avg Losing Edge by Source: "
                    + " | ".join(f"{k}={float(v):+.2f}%" for k, v in avg_loss_source.items())
                )
            )
        benchmark_payload = loss_attribution.get("focus_symbol_benchmark") or {}
        if benchmark_payload.get("candidate_id"):
            blocks.append(
                _bullet(
                    f"Benchmark Check ({loss_attribution.get('focus_symbol', 'n/a')}): "
                    f"{benchmark_payload.get('candidate_id')} "
                    f"(expectancy={float(benchmark_payload.get('expectancy_pct', 0.0)):+.2f}% | "
                    f"profit_factor={float(benchmark_payload.get('profit_factor', 0.0)):.2f} | "
                    f"trades={int(benchmark_payload.get('trade_count', 0))})"
                )
            )
        worst_episode = loss_attribution.get("worst_episode") or {}
        if worst_episode:
            blocks.append(
                _bullet(
                    f"Worst Episode: {worst_episode.get('symbol', 'n/a')} {worst_episode.get('direction', 'n/a')} | "
                    f"source={worst_episode.get('entry_source', 'unknown')} | "
                    f"edge={float(worst_episode.get('estimated_edge_pct', 0.0)):+.2f}%"
                )
            )
        for item in loss_attribution.get("observations", [])[:5]:
            blocks.append(_bullet(f"Observation: {item}"))
    holdings = financial.get("holdings", [])
    if holdings:
        for item in holdings:
            blocks.append(
                _bullet(
                    f"{item['asset']}: {float(item['quantity']):.6f} "
                    f"(Val: {float(item['value_usdt']):.2f} USDT | Weight: {float(item['weight_pct']):.1f}% | "
                    f"PnL: {float(item['unrealized_pnl_usdt']):+.2f} USDT / {float(item['unrealized_pnl_pct']):+.2f}%)"
                )
            )
    if blocked_reasons:
        blocks.append(_heading_2("Why Blocked"))
        for reason, count in blocked_reasons.items():
            blocks.append(_bullet(f"{reason}: {count}"))
    if rejection_reasons:
        blocks.append(_heading_2("Why Rejected"))
        for reason, count in rejection_reasons.items():
            blocks.append(_bullet(f"{reason}: {count}"))
    improvements = daily_review.get("improvement_directions", [])
    if improvements:
        blocks.append(_heading_2("Improvement Directions"))
        for item in improvements:
            blocks.append(_bullet(str(item)))
    if latest:
        blocks.extend(
            [
                _heading_2("Latest Decision"),
                _bullet(f"Selected Symbol: {latest.get('selected_symbol', 'n/a')}"),
                _bullet(f"Signal: {latest.get('idea', {}).get('action', 'n/a')} (score={float(latest.get('idea', {}).get('score', 0.0)):.2f})"),
                _bullet(f"Risk Decision: {latest.get('approval', {}).get('reason', 'n/a')}"),
            ]
        )
        if debate.get("risk_feedback"):
            blocks.append(_bullet(f"Debate: risk raised {debate['risk_feedback']} before final decision"))
        if debate.get("memory_guard_reason"):
            blocks.append(_bullet(f"Memory Guard: {debate['memory_guard_reason']}"))
        controls = strategy_memory.get("controls") or {}
        if controls:
            blocks.append(_bullet(f"Learning Controls: {json.dumps(controls, ensure_ascii=False)}"))
    return blocks
