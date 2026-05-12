from __future__ import annotations

import html
import json
import os
import signal
import subprocess
import sys
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from trading_agents.config import load_settings
from trading_agents.reporting import _format_stage_latency_breakdown, load_daily_summary_data, local_date_label
from trading_agents.service_manager import start_runner_service, stop_runner_service
from trading_agents.storage import build_storage_layout


settings = load_settings()
storage = build_storage_layout(settings.data_root)


def _parse_symbol_pool(raw: str) -> tuple[str, ...]:
    symbols = tuple(item.strip() for item in str(raw).split(",") if item.strip())
    return symbols or settings.observation_pool


def _runtime_settings(mode: str, symbol: str, interval: str):
    try:
        monitor_interval = float(interval)
    except (TypeError, ValueError):
        monitor_interval = settings.monitor_interval_seconds
    symbol_pool = _parse_symbol_pool(symbol)
    primary_symbol = symbol_pool[0] if symbol_pool else settings.symbol
    return replace(
        settings,
        trading_mode=mode or settings.trading_mode,
        symbol=primary_symbol,
        observation_pool=symbol_pool,
        monitor_interval_seconds=monitor_interval,
    )


class AgentController:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.mode = settings.trading_mode
        self.symbol = ",".join(settings.observation_pool) or settings.symbol
        self.interval = str(int(settings.monitor_interval_seconds))
        self.last_report: dict | None = None
        self.current_stage = "idle"
        self.current_stage_detail = "Runner service is monitored independently from this UI."
        self.stage_states = {
            "setup": "pending",
            "market_collector": "pending",
            "sentiment_collector": "pending",
            "backtester": "pending",
            "strategy_researcher": "pending",
            "strategist": "pending",
            "risk_supervisor": "pending",
            "selector": "pending",
            "executor": "pending",
            "post_trade_evaluator": "pending",
            "reporting": "pending",
        }
        self.last_cycle_started = ""
        self.last_cycle_finished = ""
        self.last_monitor_at = ""
        self.last_monitor_detail = ""
        self._runner_log_offset = 0
        self._runner_log_inode: int | None = None

    def _runner_pid(self) -> int | None:
        try:
            return int(storage.runner_pid.read_text().strip())
        except Exception:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "trading_agents.runner"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        return int(line)
            except Exception:
                pass
            return None

    def _supervisor_pid(self) -> int | None:
        try:
            return int(storage.runner_supervisor_pid.read_text().strip())
        except Exception:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "run_trading_supervisor.sh"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        return int(line)
            except Exception:
                pass
            return None

    def _pid_is_running(self, pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _pid_uptime(self, pid: int | None) -> str:
        if not self._pid_is_running(pid):
            return "stopped"
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etime="],
                capture_output=True,
                text=True,
                check=False,
            )
            uptime = result.stdout.strip()
            return uptime or "running"
        except Exception:
            return "running"

    def _runner_is_running(self) -> bool:
        pid = self._runner_pid()
        if self._pid_is_running(pid):
            return True
        if pid is not None:
            try:
                storage.runner_pid.unlink()
            except OSError:
                pass
        return False

    def start(self, mode: str, symbol: str, interval: str, *, force_restart: bool = False) -> None:
        self.mode = mode
        self.symbol = symbol
        self.interval = interval
        if self._runner_is_running() and not force_restart:
            return
        start_runner_service(_runtime_settings(mode, symbol, interval), Path(__file__).resolve().parent)
        self._reset_stages()
        self.logs.append(f"Runner active: mode={mode}, symbols={symbol}, monitor_poll={interval}s")

    def stop(self) -> None:
        stop_runner_service(settings)
        try:
            storage.runner_pid.unlink()
        except OSError:
            pass
        self.logs.append("Runner paused for debugging.")
        self.current_stage = "idle"
        self.current_stage_detail = "Runner paused. Use Resume/Apply to continue continuous trading."

    def poll(self) -> None:
        if not storage.runner_log.exists():
            return
        stat = storage.runner_log.stat()
        if self._runner_log_inode != stat.st_ino or stat.st_size < self._runner_log_offset:
            self._runner_log_offset = max(stat.st_size - 131072, 0)
            self._runner_log_inode = stat.st_ino
        with storage.runner_log.open("rb") as handle:
            handle.seek(self._runner_log_offset)
            chunk = handle.read()
            self._runner_log_offset = handle.tell()
        if not chunk:
            return
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            self.logs.append(line.rstrip())
            try:
                payload = json.loads(line)
                self._handle_payload(payload)
            except Exception:
                pass
        self.logs = self.logs[-80:]

    def _reset_stages(self) -> None:
        self.current_stage = "idle"
        self.current_stage_detail = "Runner is preparing the next cycle."
        for key in self.stage_states:
            self.stage_states[key] = "pending"

    def _handle_payload(self, payload: dict) -> None:
        event = payload.get("event")
        if event == "runner":
            self.mode = str(payload.get("mode", self.mode))
            self.interval = str(int(float(payload.get("monitor_interval_seconds", self.interval))))
            symbol_pool = payload.get("symbol_pool")
            if isinstance(symbol_pool, list) and symbol_pool:
                self.symbol = ",".join(str(item) for item in symbol_pool)
            self.current_stage = "idle"
            self.current_stage_detail = "Runner is monitoring the market."
            return
        if event == "cycle":
            status = payload.get("status", "")
            if status == "started":
                self.last_cycle_started = str(payload.get("timestamp", ""))
                self.current_stage = "setup"
                reason = str(payload.get("reason", "")).strip()
                self.current_stage_detail = f"Cycle started. {reason}" if reason else "Cycle started."
                for key in self.stage_states:
                    self.stage_states[key] = "pending"
                self.stage_states["setup"] = "active"
            elif status == "finished":
                self.last_cycle_finished = str(payload.get("timestamp", ""))
                if self.stage_states.get("setup") == "active":
                    self.stage_states["setup"] = "done"
                for key, value in list(self.stage_states.items()):
                    if value == "pending":
                        self.stage_states[key] = "skipped"
            elif status == "error":
                self.current_stage = "idle"
                self.current_stage_detail = str(payload.get("detail", "Runner hit an error and kept monitoring."))
            return
        if event == "monitor":
            self.last_monitor_at = str(payload.get("timestamp", ""))
            self.last_monitor_detail = str(payload.get("detail", ""))
            if self.current_stage == "idle":
                self.current_stage_detail = str(payload.get("detail", "Monitoring the market."))
            return
        if event == "stage":
            stage = str(payload.get("stage", ""))
            status = str(payload.get("status", ""))
            detail = str(payload.get("detail", ""))
            if stage in self.stage_states:
                if status == "running":
                    self.stage_states[stage] = "active"
                elif status == "done":
                    self.stage_states[stage] = "done"
                elif status in {"skipped", "blocked"}:
                    self.stage_states[stage] = status
                self.current_stage = stage
                self.current_stage_detail = detail or stage
            return
        self.last_report = payload

    def status(self) -> str:
        if self._runner_is_running():
            return "Running"
        return "Stopped"

    def next_run_hint(self) -> str:
        try:
            seconds = int(float(self.interval))
        except ValueError:
            return self.interval
        if seconds < 60:
            return f"{seconds} sec"
        minutes = seconds / 60
        if minutes.is_integer():
            return f"{int(minutes)} min"
        return f"{minutes:.1f} min"

    def last_run_summary(self) -> dict[str, str]:
        report = self.last_report or {}
        idea = report.get("idea", {})
        approval = report.get("approval", {})
        result = report.get("result", {})
        order = report.get("order", {})
        daily_summary = load_daily_summary_data(storage.trade_logs, local_date_label(), storage.runner_log)
        financial = daily_summary.get("financial_snapshot", {})
        blocked_reason_counts = daily_summary.get("blocked_reason_counts", {})
        stage_latency_seconds = daily_summary.get("stage_latency_seconds", {})
        stage_latency_p95_seconds = daily_summary.get("stage_latency_p95_seconds", {})
        if blocked_reason_counts:
            top_reason, top_count = next(iter(blocked_reason_counts.items()))
            blocked_hint = f"{top_reason} ({top_count})"
        else:
            blocked_hint = "No blocked proposals today"
        return {
            "last_run": self.last_cycle_finished or result.get("timestamp") or report.get("human_report", "No run yet"),
            "runner_heartbeat": self.runner_heartbeat_text(),
            "supervisor_pid": str(self._supervisor_pid() or "n/a"),
            "runner_pid": str(self._runner_pid() or "n/a"),
            "supervisor_uptime": self._pid_uptime(self._supervisor_pid()),
            "runner_uptime": self._pid_uptime(self._runner_pid()),
            "last_signal": (
                f"{report.get('selected_symbol', 'n/a')}: "
                f"{idea.get('action', 'n/a')} ({idea.get('score', 'n/a')})"
            ),
            "last_trade": (
                f"{order.get('side', 'no-trade')} {order.get('symbol', '')}".strip()
                if order else "No trade executed"
            ),
            "last_result": result.get("status") or approval.get("reason", "No result yet"),
            "blocked_today": str(daily_summary.get("blocked", 0)),
            "blocked_exchange_minimum": str(daily_summary.get("exchange_minimum_blocked", 0)),
            "blocked_top_reason": blocked_hint,
            "portfolio_value": f"{float(financial.get('total_portfolio_value_usdt', 0.0)):.2f} USDT",
            "daily_pnl": (
                f"{float(financial.get('daily_pnl_usdt', 0.0)):+.2f} USDT "
                f"({float(financial.get('daily_pnl_pct', 0.0)):+.2f}%)"
            ),
            "capital_utilization": f"{float(financial.get('capital_utilization_pct', 0.0)):.1f}%",
            "fees_today": f"{float(financial.get('daily_fees_usdt', 0.0)):.2f} USDT",
            "latency_breakdown_avg": _format_stage_latency_breakdown(stage_latency_seconds, limit=5),
            "latency_breakdown_p95": _format_stage_latency_breakdown(stage_latency_p95_seconds, limit=5),
            "llm_wake_rate": (
                f"{int(daily_summary.get('llm_wake_enabled', 0))}/"
                f"{int(daily_summary.get('llm_wake_candidates', 0))} "
                f"({float(daily_summary.get('llm_wake_rate_pct', 0.0)):.1f}%)"
            ),
            "last_debate": str((report.get("debate") or {}).get("risk_feedback") or "No active debate note"),
            "strategy_memory": str(report.get("strategy_memory_sync", {}).get("slot") or "No 12h reflection yet"),
        }

    def runner_heartbeat_text(self) -> str:
        if not self.last_monitor_at:
            return "No monitor heartbeat yet"
        try:
            beat = datetime.fromisoformat(self.last_monitor_at)
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - beat.astimezone(timezone.utc)
            seconds = max(int(delta.total_seconds()), 0)
            if seconds < 60:
                age = f"{seconds}s ago"
            else:
                age = f"{seconds // 60}m ago"
        except ValueError:
            age = self.last_monitor_at
        detail = self.last_monitor_detail or "runner heartbeat"
        return f"{age} ({detail})"

    def pipeline_steps(self) -> list[tuple[str, str, str]]:
        labels = {
            "setup": "Setup",
            "market_collector": "Market",
            "sentiment_collector": "Sentiment",
            "backtester": "Backtest",
            "strategy_researcher": "Research",
            "strategist": "Strategist",
            "risk_supervisor": "Risk",
            "selector": "Selector",
            "executor": "Executor",
            "post_trade_evaluator": "Evaluator",
            "reporting": "Reporting",
        }
        return [(key, labels[key], self.stage_states[key]) for key in labels]

    def latest_summary_content(self) -> tuple[str, str]:
        target = storage.daily_reports / f"{local_date_label()}.md"
        if not target.exists():
            return "No daily summary yet.", str(target)
        try:
            return target.read_text(), str(target)
        except Exception:
            return "Unable to read latest summary.", str(target)


controller = AgentController()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            controller.poll()
            body = self._render()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception:
            self._send_error_page()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length).decode("utf-8")
            form = parse_qs(data)
            action = form.get("action", [""])[0]
            mode = form.get("mode", [controller.mode])[0]
            symbol = form.get("symbol", [controller.symbol])[0]
            interval = form.get("interval", [controller.interval])[0]

            if action == "start":
                controller.start(mode, symbol, interval, force_restart=True)
            elif action == "stop":
                controller.stop()

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        except Exception:
            self._send_error_page()

    def log_message(self, format: str, *args) -> None:
        return

    def _send_error_page(self) -> None:
        body = "<pre>" + html.escape(traceback.format_exc()) + "</pre>"
        self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _render(self) -> str:
        status = controller.status()
        summary = controller.last_run_summary()
        latest_summary, latest_summary_path = controller.latest_summary_content()
        pipeline = "".join(
            f'<div class="step {state}"><strong>{html.escape(label)}</strong>'
            f'<span>{html.escape(state)}</span></div>'
            for _, label, state in controller.pipeline_steps()
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Trading Agents Control</title>
  <meta http-equiv="refresh" content="30">
  <style>
    :root {{
      --bg: #f5f1e8;
      --card: #fffaf0;
      --ink: #1f1d1a;
      --accent: #c46a2f;
      --muted: #6d655d;
      --good: #1d6f42;
      --bad: #8b2e2e;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: radial-gradient(circle at top, #f7e4c8, var(--bg));
      color: var(--ink);
    }}
    .wrap {{
      max-width: 920px;
      margin: 32px auto;
      padding: 0 20px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid #dbcdbb;
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 12px 40px rgba(80, 52, 22, 0.08);
      margin-bottom: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .stat {{
      background: #fff;
      border: 1px solid #e2d4c0;
      border-radius: 14px;
      padding: 14px;
    }}
    .stat strong {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 32px;
    }}
    .status {{
      font-weight: bold;
      color: {"var(--good)" if status == "Running" else "var(--bad)"};
    }}
    form {{
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 12px;
      align-items: center;
    }}
    input, select, button, textarea {{
      font: inherit;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #ccbda8;
      background: white;
    }}
    button {{
      cursor: pointer;
      background: var(--accent);
      color: white;
      border: none;
    }}
    .row {{
      display: flex;
      gap: 12px;
      margin-top: 14px;
    }}
    .ghost {{
      background: #efe5d8;
      color: var(--ink);
    }}
    .hint {{
      color: var(--muted);
      font-size: 14px;
      margin-top: 6px;
    }}
    .pipeline {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-top: 16px;
    }}
    .step {{
      border-radius: 14px;
      padding: 14px;
      border: 1px solid #e2d4c0;
      background: #fff;
    }}
    .step strong {{
      display: block;
      margin-bottom: 6px;
    }}
    .step span {{
      text-transform: uppercase;
      font-size: 12px;
      letter-spacing: 0.06em;
      color: var(--muted);
    }}
    .step.active {{
      border-color: #c46a2f;
      box-shadow: inset 0 0 0 1px #c46a2f;
      background: #fff2e5;
    }}
    .step.done {{
      border-color: #1d6f42;
      background: #eef8f1;
    }}
    .step.skipped {{
      border-color: #b8ab98;
      background: #f4eee6;
    }}
    .step.blocked {{
      border-color: #8b2e2e;
      background: #fdeeee;
    }}
    pre {{
      white-space: pre-wrap;
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Trading Agents Control</h1>
      <p>Status: <span class="status">{html.escape(status)}</span></p>
        <div class="grid">
        <div class="stat"><strong>Last Run</strong>{html.escape(summary['last_run'])}</div>
        <div class="stat"><strong>Runner Heartbeat</strong>{html.escape(summary['runner_heartbeat'])}</div>
        <div class="stat"><strong>Supervisor PID</strong>{html.escape(summary['supervisor_pid'])}</div>
        <div class="stat"><strong>Runner PID</strong>{html.escape(summary['runner_pid'])}</div>
        <div class="stat"><strong>Supervisor Uptime</strong>{html.escape(summary['supervisor_uptime'])}</div>
        <div class="stat"><strong>Runner Uptime</strong>{html.escape(summary['runner_uptime'])}</div>
        <div class="stat"><strong>Last Signal</strong>{html.escape(summary['last_signal'])}</div>
        <div class="stat"><strong>Last Trade</strong>{html.escape(summary['last_trade'])}</div>
        <div class="stat"><strong>Last Result</strong>{html.escape(summary['last_result'])}</div>
        <div class="stat"><strong>Portfolio Value</strong>{html.escape(summary['portfolio_value'])}</div>
        <div class="stat"><strong>Daily PnL</strong>{html.escape(summary['daily_pnl'])}</div>
        <div class="stat"><strong>Capital Utilization</strong>{html.escape(summary['capital_utilization'])}</div>
        <div class="stat"><strong>Fees Today</strong>{html.escape(summary['fees_today'])}</div>
        <div class="stat"><strong>Latency Avg</strong>{html.escape(summary['latency_breakdown_avg'])}</div>
        <div class="stat"><strong>Latency P95</strong>{html.escape(summary['latency_breakdown_p95'])}</div>
        <div class="stat"><strong>LLM Wake Rate</strong>{html.escape(summary['llm_wake_rate'])}</div>
        <div class="stat"><strong>Blocked Today</strong>{html.escape(summary['blocked_today'])}</div>
        <div class="stat"><strong>Blocked By Min</strong>{html.escape(summary['blocked_exchange_minimum'])}</div>
        <div class="stat"><strong>Top Why Blocked</strong>{html.escape(summary['blocked_top_reason'])}</div>
        <div class="stat"><strong>Latest Debate</strong>{html.escape(summary['last_debate'])}</div>
        <div class="stat"><strong>Strategy Memory</strong>{html.escape(summary['strategy_memory'])}</div>
      </div>
      <div class="hint">Current stage: {html.escape(controller.current_stage)}. {html.escape(controller.current_stage_detail)}</div>
      <div class="hint">This console is designed for continuous operation. Use the controls below mainly to pause for debugging or apply new settings.</div>
      <div class="pipeline">{pipeline}</div>
      <form method="post">
        <label>Mode</label>
        <select name="mode">
          <option value="bybit-demo" {"selected" if controller.mode == "bybit-demo" else ""}>bybit-demo</option>
          <option value="bybit-demo-perp" {"selected" if controller.mode == "bybit-demo-perp" else ""}>bybit-demo-perp</option>
          <option value="mock" {"selected" if controller.mode == "mock" else ""}>mock</option>
        </select>
        <label>Symbols</label>
        <input name="symbol" value="{html.escape(controller.symbol)}">
        <label>Monitor Poll (sec)</label>
        <div>
          <input name="interval" value="{html.escape(controller.interval)}">
          <div class="hint">Use comma-separated symbols. Monitor poll: {html.escape(controller.next_run_hint())}. Full decisions run on new candles, account changes, or meaningful price moves.</div>
        </div>
        <div class="row" style="grid-column: 1 / span 2;">
          <button type="submit" name="action" value="start">Apply / Resume Runner</button>
          <button class="ghost" type="submit" name="action" value="stop">Pause for Debug</button>
        </div>
      </form>
    </div>
    <div class="card">
      <h2>Latest Summary</h2>
      <div class="hint">{html.escape(latest_summary_path)}</div>
      <pre>{html.escape(latest_summary)}</pre>
    </div>
  </div>
</body>
</html>"""


def main() -> int:
    def handle_signal(_: int, __) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Trading Agents control panel running at http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
