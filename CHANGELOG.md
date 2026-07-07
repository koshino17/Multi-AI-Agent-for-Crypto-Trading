# Changelog

本文件用來記錄這個框架的重要版本演進，重點不只放「改了什麼」，也保留「為什麼要改」。

註：

- `v0.x` 版本為專案在正式上 GitHub 前的回溯式整理版本
- 早期版本內容是依據實際開發歷程、功能落地順序與系統現況回推整理
- 從這個檔案開始，後續版本建議持續沿用同樣格式維護

## Versioning Rule

- `major`: 架構層重大變更或不相容調整
- `minor`: 明顯的新能力、模組、流程升級
- `patch`: 修 bug、修文件、修部署與可用性

---

## v1.0.1 - Harden runtime trade-idea coercion for autonomous reviews

### Why

- The July 6, 2026 demo-perp runner hit repeated cycle crashes with `'dict' object has no attribute 'invalidation'` during live decision handling.
- That failure mode breaks noon-window evidence quality because TradePulse can keep monitoring while silently dropping actionable decision paths.
- For the autonomous profit-research flow, runtime payload drift must fail safe to `hold` instead of aborting the cycle.

### What Changed

- `trading_agents/main.py`
  - Added `_coerce_trade_idea(...)` so dict-shaped or malformed trade-idea payloads are normalized into `TradeIdea` objects before PO3 scoring and guard/prefilter logic touches `.action` or `.invalidation`.
  - Updated the selected-candidate rebuild path to reuse the same coercion helper instead of assuming a perfect payload shape.

- `tests/test_runtime_regressions.py`
  - Added a regression test that sends a dict-backed trade idea through `_apply_po3_deterministic_score(...)` and verifies the runtime keeps the invalidation field intact while applying the PO3 score adjustment.

### Result

- The specific July 6 payload-shape crash class should now degrade to a valid `TradeIdea` and continue the cycle instead of terminating it.
- TradePulse daily reviews should remain trustworthy even when intermediate decision payloads arrive as plain dicts.

## v1.0.0 - Autonomous TradePulse profit researcher governance

### Why

- The user explicitly delegated more TradePulse stewardship to Codex and wants autonomous daily research, not only reactive assistance.
- The previous daily review loop was useful as a summary surface, but it needed stronger rules for PO3-centered strategy research, reinforcement-learning-style policy updates, changelog discipline, and GitHub publishing.
- TradePulse also needs a clearer version boundary for the autonomous profit-research era.

### What Changed

- `AGENTS.md`
  - Added persistent TradePulse agent instructions for Codex to act as a profit researcher.
  - Defined PO3 / POC / VAH / VAL / FVG as the primary market-structure research track.
  - Added reinforcement-learning-style state / action / reward / lesson / policy update expectations.
  - Added release discipline requiring scoped code changes to update `CHANGELOG.md` and be pushed to GitHub when validation and auth allow.

- Codex personal skill: `tradepulse-profit-researcher`
  - Added the same TradePulse profit-research mission to the local Codex skill layer.
  - Added a PO3 strategy direction reference for future autonomous research runs.
  - Added release and GitHub discipline so future TradePulse code changes do not silently remain local.

- Automation: `tradepulse-daily-steward`
  - Updated the active daily automation into `TradePulse Daily Autonomous Profit Researcher`.
  - The automation now checks health, reviews PO3 market structure, applies a reinforcement-learning-style review frame, and may make scoped repo changes when evidence supports them.

### Result

- TradePulse now has a documented `v1.x.x` governance baseline for proactive Codex-led research.
- Future TradePulse code changes should be paired with changelog entries and GitHub pushes, while avoiding unrelated worktree changes.
- PO3 market-structure research and incremental policy improvement are now standing instructions instead of ad-hoc reminders.

## v0.11.59 - Fail closed when runtime launcher env is incomplete

### Why

- The project `.env` was still set to `TRADING_MODE=bybit-demo-perp`, but the synced runtime `.env` had been reduced to only `DATA_ROOT`.
- On reboot, the runtime launcher loaded the incomplete runtime `.env` and silently fell back to the config default `mock`, so launchd kept TradePulse alive while the intended demo-perp runner was effectively stopped.

### What Changed

- `scripts/launch_trading_runner.sh`
  - Added a startup guard for required runtime `.env` keys: `TRADING_MODE`, `SYMBOL`, and `OBSERVATION_POOL`.
  - If any required key is missing, the launcher now exits with a clear error instead of silently starting in `mock`.

### Result

- TradePulse should no longer disguise a broken runtime `.env` as a healthy mock runner.
- Future startup failures should be easier to diagnose from `~/Library/Logs/TradePulse/launchd-runner.log`.

## v0.11.58 - Make launchd runner follow runtime environment

### Why

- After reboot / service restarts, the LaunchAgent could keep a stale hard-coded `--mode` / `--symbol` from an older service start.
- This allowed the live service to drift into `mock` even when `.env` was set to `TRADING_MODE=bybit-demo-perp`, making trading logs and actual execution mode disagree.

### What Changed

- `trading_agents/service_manager.py`
  - The generated LaunchAgent now starts `scripts/launch_trading_runner.sh` from the synced runtime instead of hard-coding `--mode`, `--symbol`, and `--interval` in the plist.
  - The runtime sync now copies the launcher script into `~/Library/Application Support/TradePulse/runtime/scripts/`.
  - The launcher reads the runtime `.env` on every service start, so reboot behavior follows the synced TradePulse configuration.

- `scripts/launch_trading_runner.sh`
  - The launcher now resolves runner pid / lock / log paths through mode-scoped storage, keeping `mock` state out of the canonical `bybit-demo-perp` state root.
  - Removed stdout redirection into `runner.log`; launchd captures stdout/stderr separately while the runner writes structured events to `runner.log`, preventing duplicate event lines.

- `tests/test_runtime_regressions.py`
  - Added regression tests that fail if runner mode / symbol arguments are written back into the LaunchAgent plist or launcher stdout is redirected into the structured runner log.

### Result

- Rebooting the Mac should no longer resurrect an old `mock` LaunchAgent when TradePulse is configured for `bybit-demo-perp`.
- Future mode changes are centralized through `.env` + runtime sync instead of hidden inside the plist.

## v0.11.57 - Prevent duplicate runners and improve maker pilot fillability

### Why

- A stale orphan runner can keep writing decisions after launchd starts a new managed runner, inflating cycle counts and creating duplicate decision / order risk.
- Ollama was reachable, but repeated model reloads and long generations pushed some agents into timeout.
- The maker-only pilot generated orders, but 20-second post-only limits expired before producing live fills.

### What Changed

- `trading_agents/runner.py` / `trading_agents/storage.py`
  - Added a mode storage runner lock at `service/runner.lock`.
  - A second runner now enters `duplicate_blocked` instead of running trading cycles.

- `trading_agents/service_manager.py`
  - Runner start now bootouts the launch agent, terminates stale `run_tradepulse_runner.py` processes, then bootstraps a single clean launchd runner.
  - Runner stop also clears leftover runner processes.
  - Large runner stdout logs are rotated before service restart.

- `trading_agents/main.py` / `trading_agents/reporting.py`
  - Runner heartbeat and daily event-count reads now stream only recent log tails and skip full report payload lines instead of loading/parsing the entire runner log.

- `trading_agents/runner.py`
  - Large internal runner logs are rotated at process start.
  - Runner completion output now writes a compact `cycle_report` event instead of dumping the full decision report into runner / launchd logs.

- `config/strategy_library.json`
  - Increased `grid_range_reversion_maker_v1` post-only maker entry TTL from 20s to 90s so the pilot has a realistic chance to fill.

- `.env.example`
  - Added `OLLAMA_KEEP_ALIVE=30m` for the launchd Ollama service environment.

### Result

- TradePulse is less likely to double-run after restarts.
- Reporting no longer stalls on hundreds of MB of runner log payloads.
- Runner logs should stop re-growing from full-report payloads after every cycle.
- Maker pilot orders should spend enough time on the book to produce executable evidence.
- Ollama can stay warm longer between 15m cycles, reducing timeout pressure.

## v0.11.56 - Stabilize local LLM runtime and observability after reboot

### Why

- After the Mac reboot, `TradePulse` kept running but Ollama was not available, causing local LLM calls to fail with connection-refused errors and leaving fallback logic to carry most decisions.
- The launchd runner also needed a more self-contained runtime path so macOS permission / virtualenv issues would not break service startup after restarts.
- Daily review evidence needed to show whether orders expired / cancelled and whether the LLM backend was actually healthy, not just whether prompts were configured.

### What Changed

- `trading_agents/llm.py`
  - Added an Ollama `/api/tags` health check that verifies both backend reachability and configured model availability before a cycle uses LLM clients.

- `trading_agents/main.py`
  - Records `llm_health` in cycle reports and disables LLM clients cleanly when the backend is unavailable.
  - Added deterministic PO3 score adjustment for fallback / base ideas so PO3 can influence scoring even when the strategist LLM is skipped.
  - Restored the missing `time` import needed by runner sleep / monitoring paths.

- `trading_agents/reporting.py`
  - Daily summaries now include `Order Statuses` with accepted / filled / expired / cancelled / rejected counts.
  - Daily summaries now include `LLM Backend Health` so connection failures are visible in the report itself.

- `trading_agents/service_manager.py`
  - Syncs a launchd-safe runtime copy under `~/Library/Application Support/TradePulse/runtime`.
  - Uses the runtime virtualenv when available and strips stale `distutils-precedence.pth` files that can trigger Python startup errors.
  - Reloads the launch agent when the generated plist changes.

- `scripts/export_tradepulse_logs_and_prompts.py`
  - Added a reusable exporter that gathers runner logs and agent prompt traces into `~/Desktop/TradePulse_Exports/`.
  - Supports date windows such as `--since-days 7`, `--start-date`, and `--end-date`.

- `.env.example`
  - Documented the deterministic PO3 scoring knobs.

### Result

- A reboot no longer silently leaves the local LLM layer unavailable without a visible health signal.
- Daily reports now expose both order lifecycle quality and LLM backend health.
- Logs and prompts can be collected into one stable desktop folder instead of scattering ad-hoc exports around the Desktop.

## v0.11.55 - Soft-integrate PO3 into decisions and reporting

### Why

- PO3 / POC / VAL / FVG had reached the snapshot and report layers, but they still had almost no influence on actual decision quality.
- We needed a safer bridge between “interesting structure features” and “live behavior” than jumping straight to hard decision gates.
- Daily reports also lacked a practical way to see whether specific PO3 phases were helping or hurting execution quality.

### What Changed

- `trading_agents/agents.py`
  - Strategist prompts now receive a compact deterministic `market_structure` briefing.
  - Added a soft PO3-based score adjustment layer:
    - downside expansion de-emphasizes fresh longs,
    - upside expansion de-emphasizes fresh shorts,
    - accumulation de-emphasizes chasing value-area extremes,
    - aligned expansion can add a small positive score bump.

- `trading_agents/reporting.py`
  - Added `PO3 Phase Performance` to daily reports so we can inspect proposals, approvals, executions, closed episodes, win rate, and approximate after-fee expectancy by `po3_phase_hint`.
  - Tightened shadow benchmark promotion hygiene so candidates with fewer than 8 trades stay research-only and do not steer promotion logic.
  - Improved inferred account-state close attribution so same-window unlogged openings are less likely to be mislabeled as `carry_in`.

- `tests/test_runtime_regressions.py`
  - Added semantic regression tests for `_infer_po3_phase_hint` and `_build_fvg_features`, not just presence checks.

### Result

- PO3 is no longer just “shown in the report”; it now has a bounded, reviewable influence on decision scoring.
- Daily reports can start telling us which PO3 phases are producing useful participation versus noisy proposals.
- Low-sample benchmark spikes are less likely to distort strategy promotion decisions.

## v0.11.53 - Add deterministic market-structure features

### Why

- We wanted to explore PO3 / POC / VAL / FVG ideas without turning `TradePulse` into an LLM-driven pattern-guessing engine.
- The safest first step was to compute these as deterministic snapshot features, then expose them in reports and traces before letting them influence live execution.

### What Changed

- `trading_agents/models.py`
  - Extended `MarketSnapshot` with deterministic market-structure fields:
    - `poc_*`
    - `value_area_*`
    - `nearest_*_fvg_distance_bps`
    - `fvg_fill_ratio`
    - `po3_phase_hint`

- `trading_agents/exchange.py`
  - Added lightweight value-profile, fair-value-gap, and PO3-hint feature builders.
  - Wired those features into spot, perp, and ccxt-backed market snapshots.

- `trading_agents/main.py`
  - Decision payloads now carry a `market_structure` section so runtime logs and downstream artifacts can inspect the new features.

- `trading_agents/reporting.py`
  - `Latest Decision` now shows a compact `Market Structure` and `FVG Context` summary.

- `tests/test_runtime_regressions.py`
  - Added a regression test to ensure the new snapshot features are present and non-empty.

### Result

- `TradePulse` now records deterministic PO3 / POC / VAL / FVG-style structure hints in the snapshot layer.
- We can inspect them in daily reports and traces before deciding whether they deserve a role inside fallback guards or execution logic.

## v0.11.54 - Use market-structure features as fallback guards

### Why

- Once the deterministic PO3 / POC / VAL / FVG features were visible, the next safe step was to use them as guardrails instead of full strategy drivers.
- We especially wanted to stop fallback-style longs and shorts from chasing likely false breaks around value-area edges without real flow confirmation.

### What Changed

- `trading_agents/main.py`
  - Added a market-structure guard that converts weak fallback breakouts into `hold` when:
    - price is already stretched beyond value area,
    - PO3 hint looks like manipulation,
    - nearby FVG structure suggests an unfinished imbalance,
    - and trade delta / volume confirmation is still weak.
  - Wired the guard ahead of the existing fallback entry / neutral-base guards.

- `trading_agents/config.py`
  - Added dedicated `MARKET_STRUCTURE_GUARD_*` settings so thresholds can be tuned without touching code.

- `.env.example`
  - Documented the new market-structure guard knobs.

- `tests/test_runtime_regressions.py`
  - Added a regression test to ensure a weak long above value area is blocked by the new guard.

### Result

- `TradePulse` still treats PO3 / POC / VAL / FVG as deterministic hints, but it now actively uses them to avoid some obvious false-break fallback entries.

## v0.11.50 - Split mock state from live bybit runtime

### Why

- `mock` and `bybit-demo-perp` runners were both writing into the same `DATA_ROOT`, which let mock decisions and reports bleed into the live noon-window summaries.
- This made recent daily reports look flat at `500 USDT` with `0 trades`, even though the real question was how the bybit-demo-perp line had actually performed.
- The contamination was not only at the file level; some reporting paths were also reloading mode information from `.env`, which could silently regenerate summaries under the wrong trading mode.

### What Changed

- `trading_agents/storage.py`
  - Added mode-aware storage root resolution.
  - Kept `bybit-demo-perp` on the canonical root, while non-live modes such as `mock` now write under their own scoped subdirectories.

- `trading_agents/main.py`
  - `execute_cycle()` now builds storage from the current cycle mode, not just the shared root.
  - Daily summary generation, daily review rebuilds, artifact refreshes, and strategy-reflection context now explicitly use the active cycle mode and storage root instead of falling back to `.env`.

- `trading_agents/runner.py`
  - Runner PID/log/state now follow the mode-aware storage root so concurrent modes stop sharing the same live state files.

- `trading_agents/reporting.py`
  - `load_daily_summary_data()` and `build_daily_summary()` now accept explicit `trading_mode` and `storage_root` overrides.
  - This keeps rebuilds and report regeneration aligned with the actual runner that produced the records.

- `trading_agents/service_manager.py` / `trading_agents_web.py`
  - Service/UI status paths now resolve storage by mode as well, so polling and PID/log lookups follow the correct runtime lane.

- `tests/test_runtime_regressions.py`
  - Added regression coverage to ensure `bybit-demo-perp` keeps the canonical root while `mock` is forced into its own scoped state directory.

- `trading_agents/agents.py`
  - Selector no longer re-centers a no-trade cycle on a watch-only symbol when nothing is executable; it now keeps the fallback/live focus symbol in place for reporting and postmortem consistency.

## v0.11.49 - Tighten policy exits and unblock aligned trend entries

### Why

- Recent daily reviews still showed three recurring trade-quality issues:
  - `policy_exit` was sometimes producing tiny handoff-window closes with almost no edge.
  - `projected available balance too low of equity` was still blocking add-ons mainly when a position was already open and moving in the right direction.
  - `long failure -> short flip` remained too slow in clear down legs.
- In addition, `FEE_HURDLE_MULTIPLIER` looked configurable in demo perp mode, but the risk layer was silently clamping it to `<= 1.0`, which meant the knob could not actually be used to make edge filters stricter.

### What Changed

- `trading_agents/main.py`
  - Made report-window handoff exits more selective by requiring a stronger weak-edge condition before de-risking carry-in positions near the noon anchor.

- `trading_agents/agents.py`
  - Added a faster bearish fallback path for `long fail -> short flip` style conditions when signal, momentum, volume, and order-flow all turn down together.
  - Reworked perp available-balance gating so aligned add-ons can use a dynamically relaxed minimum available-balance threshold instead of being blocked by the same static rule as fresh entries.
  - Removed the demo-perp clamp that forced `FEE_HURDLE_MULTIPLIER` to `<= 1.0`, so the configured multiplier now genuinely affects cost-aware approval.

- `tests/test_runtime_regressions.py`
  - Added regression coverage for:
    - fee-hurdle multiplier enforcement in bybit-demo-perp
    - fast short-flip fallback generation
    - dynamic balance-guard relaxation for aligned add-ons

## v0.11.48 - Reduce observe-only lockup from heavy prompts and strict restore rules

### Why

- Recent `agent_traces` showed strategist / selector / risk prompts were still carrying more context than they needed, especially around raw `strategy_memory` and selector account payloads.
- At the same time, strategy-learning restore logic still expected equity to climb back close to the original `500 USDT` baseline before relaxing controls, which made `base_only` / preservation modes sticky after a drawdown.
- Under low-sample windows, `capital_preservation_pilot` and `fallback_entry_mode=base_only` could also stack together, leaving the system overly eager to stay in observe-only mode.

### What Changed

- `trading_agents/config.py` / `.env.example` / `README.md`
  - Raised the default fast-path `LLM_TIMEOUT_SECONDS` from `18` to `45`.
  - Relaxed `STRATEGY_LEARNING_RESTORE_EQUITY_RECOVERY_RATIO_PCT` from `99` to `95`.
  - Added recovery knobs so restore can also happen when recent windows have real fills and live expectancy has stabilized:
    - `STRATEGY_LEARNING_RESTORE_RECENT_WINDOWS`
    - `STRATEGY_LEARNING_RESTORE_RECENT_ACCEPTED_ORDERS`
    - `STRATEGY_LEARNING_RESTORE_EXPECTANCY_FLOOR_PCT`

- `trading_agents/agents.py`
  - Replaced raw `strategy_memory` dumps in strategist / risk / selector prompts with a compact `strategy_memory_brief`.
  - Slimmed selector payloads so they carry only the account fields needed for choice, instead of the whole account blob.
  - Prevented low-sample pilot mode from stacking `fallback_entry_mode=base_only` on top of `capital_preservation_pilot`; pilot now stays bounded without double-locking new entries.

- `trading_agents/main.py`
  - Strategy reflection context now tracks recent live expectancy per window.
  - Restore readiness now supports an activity-based path in addition to the old equity-floor path, so the system can unlock once it has recent accepted orders and expectancy is no longer deeply negative.

- `tests/test_runtime_regressions.py`
  - Added regression coverage to ensure low-sample pilot mode does not keep `base_only` stacked on top of it.

## v0.11.47 - Fix unlogged perp close attribution in daily reports

### Why

- A completed noon-window report showed `Daily PnL +12.84 USDT`, but almost all of it was stuck inside `PnL Bridge residual` instead of appearing in `Realized PnL`.
- The root cause was that `Trade Review` and the financial snapshot only trusted accepted close logs. When a perp position disappeared from account state without a matching accepted reduce-only record, the equity change landed in the account curve but the reporting layer dropped the close.

### What Changed

- `trading_agents/reporting.py`
  - Added account-state transition inference for perp positions so `non-flat -> flat` transitions can be reconstructed as `account_state_inferred` closes.
  - `Trade Review` now includes those closes as `unlogged_in_window` episodes instead of silently omitting them.
  - The noon-window financial snapshot now adds inferred realized PnL for those closes, which materially reduces `PnL Bridge residual`.
  - Restored the missing `fmean` import so full daily-report rebuilds no longer crash during control-impact rendering.

- `tests/test_runtime_regressions.py`
  - Added a regression test that simulates an unlogged short close and verifies both the inferred episode and the reduced residual path.

## v0.11.46 - Stabilize review/reflection prompt execution

### Why

- The new agent trace archive immediately exposed that `DailyReviewAgent` and `StrategyReflectionAgent` were still using the same short timeout as intraday decision calls.
- That meant the traces were useful, but they often captured `timed out` rather than a usable review or reflection payload, which blocked the whole point of making the postmortem layer easier to inspect.

### What Changed

- `trading_agents/config.py`
  - Added `strategy_review_llm_timeout_seconds`, defaulting to `60.0`, so slower review-style prompts can be configured separately from fast intraday calls.

- `trading_agents/main.py`
  - Added a dedicated review/reflection `OllamaClient`.
  - `DailyReviewAgent` and `StrategyReflectionAgent` now use this longer-timeout client, while faster strategist / risk / selector calls continue using the regular `LLM_TIMEOUT_SECONDS`.

- `.env.example` / `README.md`
  - Added `STRATEGY_REVIEW_LLM_TIMEOUT_SECONDS=60` to the documented runtime configuration so repo settings and live runtime knobs stay aligned.

## v0.11.45 - Add agent trace archive and machine-readable ground truth

### Why

- We had richer daily reports, but when a human wanted to inspect “what prompt did the agent actually send?” there was still no direct artifact to open.
- Market-path review already existed in the daily report, but there was no machine-readable `ground truth` bundle or hindsight-style oracle artifact that could be reused by research or prompt-improvement workflows.

### What Changed

- `trading_agents/llm.py` / `trading_agents/storage.py`
  - Every LLM-backed agent call now writes an `agent trace` archive under `service/agent_traces/<date_label>/`.
  - Each agent gets:
    - `<agent>.md` with timestamped prompt/response sections
    - `<agent>.jsonl` with structured trace payloads
  - Trace labels follow the same noon-window date anchor as daily reports.
- `trading_agents/reporting.py`
  - Daily summaries now expose artifact paths for:
    - `Agent Trace Archive`
    - `Ground Truth`
    - `Oracle Postmortem`
  - Added machine-readable + markdown artifact writers for:
    - `service/ground_truth/<date_label>.json|md`
    - `service/oracle_postmortems/<date_label>.json|md`
  - Daily reports now include a `Research Artifacts` section so these paths are visible without digging through the filesystem.
- `trading_agents/main.py`
  - Report finalization now refreshes the above artifacts for both active and completed noon windows whenever daily reports are rebuilt.
- `tests/test_runtime_regressions.py`
  - Added regression coverage for noon-anchor trace labeling and for writing ground-truth/oracle artifacts.

## v0.11.44 - Turn daily reflection into bounded experiments with control-impact reporting

### Why

- TradePulse already had noon-window reviews and 12h strategy reflection, but most of the learning loop still ended at “write guidance and tweak controls”.
- That made it too hard to tell whether a reflection change actually helped: there was no explicit experiment lifecycle, no minimum-sample guard against overreacting, and no daily report section showing how control changes affected participation, fee drag, or policy exits.
- Strategy research also accepted `strategy_memory` as input but barely used it, so reflection had limited influence over candidate selection.

### What Changed

- `trading_agents/agents.py`
  - Strategy reflection now applies a low-sample guard: if accepted orders or closed episodes are below threshold, only a bounded subset of tunable controls can change in one window.
  - Reflection now emits a structured `experiment` payload with:
    - `experiment_id`
    - `trigger`
    - `success_metrics`
    - `ttl_windows`
    - `control_deltas`
    - `sample_guard_active`
- `trading_agents/research.py`
  - Strategy research now uses `strategy_memory.controls` to bias candidate selection toward the current benchmark-watch or pilot candidate, instead of ignoring memory almost entirely.
- `trading_agents/reporting.py`
  - Daily summaries now compute a `Control Impact` block by comparing the current noon window with the previous one.
  - Reports now show:
    - changed controls
    - active experiment metadata
    - accepted-rate delta
    - hold-ratio delta
    - cost-impact delta
    - accepted policy-exit delta
    - average hold-bars delta for closed episodes
- `trading_agents/main.py` / `trading_agents/strategy_memory.py`
  - Strategy memory now persists `experiment` alongside controls and reflection context.
  - Daily-review fingerprinting now includes experiment identifiers so completed reviews rebuild when the experiment state changes.
- `tests/test_runtime_regressions.py`
  - Added regression coverage for low-sample reflection gating and memory-aware strategy research selection.
- `.env.example` / `trading_agents/config.py`
  - Added knobs for:
    - `STRATEGY_LEARNING_MIN_ACCEPTED_ORDERS`
    - `STRATEGY_LEARNING_MIN_CLOSED_EPISODES`
    - `STRATEGY_LEARNING_LOW_SAMPLE_MAX_TUNABLE_CHANGES`
    - `STRATEGY_LEARNING_COOLDOWN_STEP_LIMIT`
    - `STRATEGY_LEARNING_EXPERIMENT_TTL_WINDOWS`

## v0.11.43 - Realign strategy documentation with the current live baseline

### Why

- The repo had already shifted its real live baseline to `grid_range_reversion_maker_v1`, but several top-level documents were still describing `donchian_adx_perp_v1` as the active strategy.
- That mismatch made it too easy to misread reports, research output, and runtime behavior.
- A few repo docs were also still using local absolute file paths, which are acceptable in Codex chat responses but wrong inside portable project documentation.

### What Changed

- `README.md`
  - Updated the live strategy / benchmark section so it matches the current `grid_range_reversion_maker_v1` live baseline.
- `CURRENT_LIVE_STRATEGY_TRUTH_TABLE.md`
  - Rewritten to reflect the current maker-grid baseline instead of the older Donchian baseline.
  - Replaced local absolute path links with repo-portable references.
- `EXCHANGE_AND_SIMULATION_OPTIONS.md`
  - Clarified that OKX remains a future / research option and is not currently exposed as a runtime mode.

## v0.11.42 - Fix pilot crashes, web runner overrides, perp monitoring, and Python runtime consistency

### Why

- A fresh code scan found several real operational issues that were too close to the live trading path to leave as paper findings.
- The most urgent ones could either crash risk approval in pilot mode, ignore Web UI runner settings, or make the runtime depend on a different Python environment than the one we install into.
- There was also a quiet execution-quality regression: maker-style limit orders were losing market microstructure context because the selected candidate snapshot never made it through to the executor path.

### What Changed

- `trading_agents/agents.py`
  - Fixed the pilot-mode `warnings` initialization order so `capital_preservation_pilot` no longer risks an `UnboundLocalError` during risk review.
- `trading_agents_web.py`
  - The Web UI now builds an override settings object from the submitted `mode`, `symbol`, and `interval` values before restarting the runner, so the form actually controls the launched process.
- `trading_agents/runner.py`
  - Monitor snapshots now track perp account state with `net_position`, aligning the heartbeat path with full-cycle account snapshots.
- `trading_agents/main.py`
  - The selected candidate's live market snapshot is now preserved out-of-band and passed into order building and protection sync, so maker-style limit pricing can use bid/ask context instead of degrading to `last_price`.
- `trading_agents/service_manager.py`
  - Runner launchd and detached fallback paths now prefer the project `.venv` interpreter when available instead of hard-coding `/usr/bin/python3`.
- `Launch Trading Agents.command`, `scripts/launch_trading_runner.sh`, `scripts/run_trading_supervisor.sh`
  - Startup scripts now prefer `.venv/bin/python3` when present.
- `README.md`
  - Clarified that the project is macOS-first for the desktop launcher / launchd path, while Linux support is command-line oriented rather than parity with the macOS helpers.
- `tests/test_runtime_regressions.py`
  - Added regression coverage for pilot-mode risk review, Web UI settings overrides, and perp monitor account-state tracking.

## v0.11.41 - Escalate repeated hold-only windows into tiny maker-grid pilot mode

### Why

- TradePulse had started defending capital better, but it was still spending too many completed windows in an almost pure observe-only state.
- That created a new failure mode: not overtrading, but learning too slowly because research conclusions were never being converted into bounded live validation.
- We needed a rule that says: if recent windows are dominated by holds, accepted trades stay near zero, and research remains aligned with the same positive-cost-adjusted candidate, open a tiny maker-only pilot instead of staying frozen.

### What Changed

- `trading_agents/main.py`
  - Strategy reflection context now tracks low-participation windows using hold ratio, accepted order count, and proposal activity across the recent noon-to-noon lookback set.
  - Reflection context now also passes the latest idle-time strategy research recommendation into the learning loop.
- `trading_agents/agents.py`
  - Strategy reflection can now activate `capital_preservation_pilot` not only during drawdown preservation mode, but also after repeated high-hold / low-trade windows when the same maker candidate remains aligned between research and live benchmark evidence.
  - Reflection summaries now explicitly mention `low_participation_windows` / `low_participation_streak` and record when a tiny pilot is justified.
- `.env.example` / `trading_agents/config.py`
  - Added explicit knobs for:
    - `STRATEGY_LEARNING_PILOT_HOLD_RATIO_THRESHOLD`
    - `STRATEGY_LEARNING_PILOT_LOW_PARTICIPATION_WINDOWS`
    - `STRATEGY_LEARNING_PILOT_MAX_ACCEPTED_PER_WINDOW`

## v0.11.40 - Unify shadow-candidate selection across reports and learning memory

### Why

- `Shadow Benchmark Watch` was choosing a candidate from the single-window external benchmark snapshot, while idle-time strategy research used multi-window + validation-symbol aggregation, and `strategy_memory` leaned on repeated benchmark leaders.
- That meant the three most important places for strategy direction could disagree in the same report window, which made TradePulse look indecisive even when the underlying data was valid.
- We needed one adjudication path so that reports, learning controls, and promotion logic all talk about the same candidate unless there is a clear reason not to.

### What Changed

- `trading_agents/reporting.py`
  - Shadow watch selection now considers idle-time strategy research recommendations before falling back to the single-window external benchmark leader.
  - Daily summary loading now exposes a synthesized `benchmark_watch_candidate_current` so downstream consumers can use the same adjudicated candidate.
  - Daily reports now show `Selection Source` inside `Shadow Benchmark Watch` to make it obvious whether the active candidate came from strategy research or the external benchmark leader.
- `trading_agents/main.py`
  - Strategy reflection context now prefers the synthesized benchmark-watch candidate from the loaded daily summary instead of always defaulting to `external_benchmarks.top_by_symbol`.
  - This keeps `strategy_memory`, daily review, and shadow-watch reporting aligned on the same promotion candidate.

## v0.11.39 - Add bounded maker-grid pilot mode under capital preservation

### Why

- `TradePulse` had already shifted its research focus toward `grid_range_reversion_maker_v1`, and recent cost-aware tournaments kept supporting that direction.
- But the live system was still stuck in an all-or-nothing posture: either fully frozen under `capital_preservation`, or eventually forced to restore normal entry mode all at once.
- That made the framework too passive. We needed a middle state where TradePulse could test the strongest candidate in the real runtime with tightly bounded exposure instead of remaining permanently paper-only.

### What Changed

- `trading_agents/agents.py`
  - Strategy reflection can now promote a repeated positive benchmark leader into `capital_preservation_pilot` mode when post-cost expectancy, profit factor, and streak requirements are met.
  - The learning controls now support:
    - `entry_mode=capital_preservation_pilot`
    - `pilot_candidate_id`
    - `pilot_max_position_pct`
  - Risk sizing now respects that pilot mode by capping new exposure to a smaller percentage of buffered available balance and only for the approved maker-style candidate.
- `trading_agents/main.py`
  - The memory-entry guard now allows a bounded exception path: maker-only entries for the approved pilot candidate can pass while all other new exposure remains blocked.
- `.env.example` / `trading_agents/config.py`
  - Added explicit pilot-tuning settings for benchmark streak, minimum expectancy, minimum profit factor, and pilot max position sizing.

## v0.11.38 - Keep noon reports aligned with refreshed strategy memory

### Why

- The daily automation correctly identified that completed noon-window reports could lag behind the newly refreshed `strategy_memory.json`.
- In practice, that meant a completed report could still show stale learning controls or action items from the pre-refresh decision state, even though the persistent strategy memory had already moved on.
- That made the automation look unreliable: it had diagnosed the right problem, but the live report still lagged one cycle behind.

### What Changed

- `trading_agents/reporting.py`
  - Daily summary loading now includes the current persisted strategy memory from `service/strategy_memory.json`.
  - Completed reports now prefer the current persisted learning controls when rendering `Latest Decision` and review context instead of relying only on the last embedded decision payload.
- `trading_agents/agents.py`
  - `DailyReviewAgent` now reads `strategy_memory_current` from the loaded summary when available, so fallback daily reviews stop lagging behind refreshed learning controls.
- `trading_agents/main.py`
  - The daily review fingerprint now includes key strategy-memory controls.
  - When the noon strategy reflection updates `strategy_memory.json`, TradePulse immediately regenerates the completed daily review and rebuilds both completed and active daily markdown files in the same run.

## v0.11.37 - Surface idle-time strategy research in live daily reports

### Why

- The daily automation had started running a separate idle-time strategy research cycle, but the live daily reports still did not surface that result.
- That made the automation look like it had "reviewed but not written", even when fresh research evidence already existed in `strategy_research_latest.json`.
- Users also needed a clearer distinction between:
  - repo-side fixes that changed report generation logic
  - and live daily markdown files that had actually been regenerated with the new research section

### What Changed

- `trading_agents/reporting.py`
  - Daily summary loading now reads the latest idle-time strategy research snapshot from `service/strategy_research_latest.json`.
  - Daily reports now render a new `Idle-Time Strategy Research` section, including:
    - generation time
    - focus / validation symbols
    - lookback windows
    - current recommendation and rationale
    - aggregate leader statistics
    - per-window tournament leaders
- Live state
  - Rebuilt the active live daily markdown files so the new section appears immediately in the live reports instead of waiting for a future code path to regenerate them.

## v0.11.36 - Promote maker-style grid as the live baseline and add passive limit execution

### Why

- `donchian_adx_perp_v1` has continued to lose money in the current `SOL/USDT` regime, and the recent cost-aware strategy tournament no longer supports treating it as a trustworthy live default.
- The strongest research candidate is no longer “plain grid”, but specifically `grid_range_reversion_maker_v1`, whose edge depends on **maker-style execution** rather than the current taker-only market-order path.
- Switching strategy names without switching execution mechanics would be misleading: a `maker` backtest routed through `Market` live orders would simply recreate the same friction trap that already invalidated the taker grid variant.

### What Changed

- `config/strategy_library.json`
  - Promoted `grid_range_reversion_maker_v1` to the live `base_strategy`.
  - Kept `donchian_adx_perp_v1` in the library as an internal reference candidate instead of the live source of truth.
  - Added explicit execution metadata so live strategy selection now carries its intended entry style (`limit` + `post_only` for maker-style grid, `market` for Donchian).
- `trading_agents/research.py`
  - Rebuilt `StrategyResearchAgent` as a **generic rule-strategy evaluator** instead of a Donchian-only researcher.
  - Strategy replay now dispatches through the shared benchmark signal generators and uses the same candidate-level TP/SL and cost assumptions used in research.
  - The selected strategy now carries a `selected_execution_profile`, so downstream execution can honor the strategy’s intended order style.
- `trading_agents/agents.py`
  - `ExecutorAgent.build_order(...)` now supports strategy-aware execution metadata, including `limit_price`, `post_only`, `entry_ttl_seconds`, and liquidity style.
- `trading_agents/exchange.py`
  - Added passive `Limit` order support for Bybit demo perp orders, including price normalization, short-lived post-only order handling, order-status polling, and cancel-on-timeout behavior.
  - This gives `TradePulse` a real execution path for `maker`-style strategies instead of only a backtest assumption.
- `trading_agents/main.py`
  - Strategy research serialization/deserialization now preserves the selected execution profile.
  - Live order submission now passes the selected strategy’s execution profile and market snapshot into the executor.
- `config/external_benchmark_library.json`
  - Updated the external benchmark baseline ID to match the new live baseline, so shadow comparisons and promotion logic stop treating the old Donchian baseline as the live source of truth.
- `trading_agents/reporting.py`
  - Daily postmortems now compare symbol benchmark leaders against the **current live baseline**, not a hard-coded Donchian default.

## v0.11.35 - Align daily benchmark reviews with the completed noon window

### Why

- The completed `Daily Summary` could still mix in a newer external benchmark snapshot generated **after** the report window ended.
- That created confusing contradictions where `Strategy Review`, `strategy_memory`, `Shadow Benchmark Watch`, and `External Benchmarks` could appear to disagree about which candidate was actually leading.
- The cached `daily_strategy_review-YYYY-MM-DD.json` also stayed frozen as long as the file existed, even after the underlying daily summary logic changed.

### What Changed

- `trading_agents/reporting.py`
  - Completed daily summaries now load the most recent benchmark snapshot **at or before the window end**, instead of always using the latest state file.
  - Shadow promotion streak tracking now respects that same cutoff, so completed reports no longer leak future benchmark evidence into past windows.
- `trading_agents/main.py`
  - Added a `daily_strategy_review` fingerprint so the stored review is regenerated whenever the underlying completed summary materially changes.
  - This keeps `Strategy Review` aligned with the corrected report window instead of silently reusing stale conclusions.

## v0.11.34 - Tighten carry-in de-risk and add noon-handoff weak-position exits

### Why

- The daily automation had correctly diagnosed repeated carry-in drag, but some of its actual strategy-side changes were left behind in a worktree instead of making it into the live repo/runtime.
- Recent noon windows kept showing the same unhealthy pattern: a weak carry-in position would survive into the next report window, then get closed by a familiar `stagnation exit` after already becoming PnL drag.
- If TradePulse is really going to grow rather than just describe the same failure, it needs to become more aggressive about not carrying weak positions across the noon handoff.

### What Changed

- `trading_agents/agents.py`
  - Strengthened the learning response to repeated carry-in losses by tightening `hold_bars_scale`, `stagnation_bars_scale`, and `stagnation_pnl_scale` more aggressively when `carry_in_mode=de_risk` is triggered.
  - Preserved a stricter de-risk posture while the negative streak is still active instead of relaxing too early.
- `trading_agents/config.py`
  - Added explicit settings for a noon report-window handoff exit:
    - `INTRADAY_REPORT_HANDOFF_EXIT_ENABLED`
    - `INTRADAY_REPORT_HANDOFF_EXIT_MINUTES`
    - `INTRADAY_REPORT_HANDOFF_MAX_PNL_PCT`
- `trading_agents/main.py`
  - Added a new policy-exit path that proactively closes weak carry-in positions inside the final minutes before the noon anchor, instead of letting them drift into the next report window by default.

## v0.11.33 - Make carry-in reporting explicit and relax grid shadow promotion gating

### Why

- `TradePulse` recently showed an unhealthy pattern where the daily report could display an open carry-in position in `Current Portfolio`, but `Trade Review` had no corresponding open episode in the same window.
- That made it harder to tell whether the system had really opened a new position, or whether the position simply existed in account state without a matching accepted opening trade in local logs.
- At the same time, `grid_range_reversion_v1` had been repeatedly outperforming the live baseline on `SOL/USDT`, but the shadow promotion logic was still too strict to let that evidence accumulate into a meaningful streak.

### What Changed

- `trading_agents/reporting.py`
  - `Trade Review` now accepts the `financial_snapshot` and can synthesize open carry-in episodes directly from live holdings when no accepted opening trade exists in the local window logs.
  - `Current Portfolio` now labels these cases explicitly as `carry_in_unlogged`, with a plain-language reason instead of silently leaving the origin ambiguous.
  - The shadow promotion gate now uses candidate-specific qualification thresholds.
  - `grid_range_reversion_v1` can begin accumulating a promotion streak with a more realistic trade-count and edge threshold, instead of being held back by the same gate used for more active directional strategies.

## v0.11.32 - Live expectancy and cost diagnostics

### Why

- 使用者希望 `TradePulse` 的每日 postmortem 不只描述成敗，還要直接顯示策略最核心的概率與成本指標。
- 既有報表雖然有 benchmark 的 `expectancy` / `profit factor`，但 live 策略自己的 daily review 仍缺少：
  - 單筆期望值（expectancy）
  - live profit factor
  - 成本侵蝕比例（Cost Impact Ratio）
- 若沒有這些指標，就很難快速判斷：
  - 是策略本身沒 edge
  - 還是 fees 把 edge 吃掉
  - 或只是短期運氣噪音

### What Changed

- `trading_agents/reporting.py`
  - `Loss Attribution` 現在會新增 live closed-episode 指標：
    - `Live Trade Expectancy`
    - `Live Profit Factor`
    - `Cost Impact Ratio`
    - `Closed Episodes`
    - `win rate / avg win / avg loss`
  - 這些指標目前以 **已平倉 episode** 為口徑，避免和 benchmark 或單筆 accepted orders 混在一起
  - `Cost Impact Ratio` 目前先採 **fees-only** 版本；funding fee 尚未接進 accounting 時，報表會明確標示 funding 尚未整合
- `trading_agents/notion_sync.py`
  - Notion `Daily Review` 的 `Loss Attribution` 區塊同步補上上述 live 指標，避免本地與 Notion 報表口徑分裂

## v0.11.30 - Noon-to-noon report windows and carry-in attribution

### Why

- Daily reports were still published around Taiwan noon, but the underlying statistics were cut on calendar days (`00:00 -> now`), which made the report window mismatch the intended review cycle.
- This caused post-noon trades to look like they belonged to the wrong daily report.
- When a position was already open before the first accepted trade in a report window, `Trade Review` and `Loss Attribution` could miss that carry-in context and make a close look like an isolated trade.

### What Changed

- `trading_agents/reporting.py`
  - daily report windows now use **Taiwan time `12:00 -> next day 12:00`** for:
    - trade-log aggregation
    - runner heartbeat counts
    - daily PnL basis labeling
  - daily reports now print the active report window explicitly at the top
  - `Trade Review` can now reconstruct `carry_in` episodes when a reduce-only close appears without a matching opening trade inside the same report window
  - `Loss Attribution` now distinguishes:
    - `carry-in` closes
    - newly opened-and-closed episodes
- `trading_agents/main.py`
  - daily review / strategy reflection flow now uses the completed noon-window summary, instead of accidentally mixing it with the active in-progress window
- `README.md`
  - 補上 noon-to-noon daily report 規則與 `carry-in` 倉位說明

### Notes

- `2026-04-29` 現在代表 `2026-04-28 12:00 -> 2026-04-29 12:00`
- `2026-04-30` 則代表 `2026-04-29 12:00 -> 2026-04-30 12:00`

## v0.11.26 - SOL-focused external benchmark variants

### Why

- 使用者希望把 Gemini 對 `SOL/USDT` 的 Donchian 優化建議，收進 `TradePulse` 的研究流程，但不要直接污染 live 主策略
- 現有 external benchmark library 只有單一 `Donchian 20 + ADX` 基線，還不夠拿來比較：
  - `10 / 14 / 20` channel period
  - `Keltner-filtered breakout`
  - `ATR + midline` 型出場
- 需要把這些候選正式做成 benchmark candidates，讓 daily report / shadow research 能用真實 replay 結果討論，而不是停留在口頭建議

### What Changed

- `config/external_benchmark_library.json`
  - 新增：
    - `donchian_adx_fast_14_v1`
    - `donchian_adx_fast_10_v1`
    - `donchian_adx_keltner_v1`
    - `donchian_adx_atr_midline_exit_v1`
- `trading_agents/external_benchmarks.py`
  - benchmark engine 現在支援：
    - `Donchian + ADX` 的快速週期變體
    - `Keltner Channel` 過濾 breakout
    - `ATR + Donchian midline` 的研究型 exit profile
  - 新增 EMA / ATR 輔助計算，讓這些候選能真的跑 replay，不只是設定檔佔位
- `README.md`
  - 補上目前 external benchmark 已支援的研究候選
  - 補上 `Shadow Benchmark Watch` 的說明
  - 明確標記 `funding rate` 仍屬後續 research item，尚未接進 live 或 benchmark
- `reporting.py` / `agents.py`
  - daily report 與 `Strategy Review` 現在會新增 `Shadow Benchmark Watch`
  - 會直接比較 live baseline 與指定 shadow candidate 在同標的上的：
    - expectancy
    - profit factor
    - cumulative return
    - trade count
  - 並輸出 `keep_shadow_watch / watch_streak_building / promotion_candidate / no_upgrade_signal`
  - promotion 判斷會看近期 benchmark 連續快照的 streak，而不是單日偶發領先

## v0.11.24 - Always-on launchd runner

### Why

- 使用者希望 `TradePulse` 不應再依賴 Codex 會話或父程序；只要電腦開機且恢復連網，就應該持續自行執行
- 先前的 background runner 曾經被掛在 Codex `app-server` 底下，會造成 usage limit / 父程序退出時一起停掉，這不符合真正常駐服務的預期
- macOS `launchd` 直接從 `Documents` 和外接磁碟的既有 `DATA_ROOT` 啟動時，會遇到 TCC / 權限限制，導致 service 無法穩定寫入 pid/lock 狀態檔

### What Changed

- `service_manager.py`
  - 新增 `~/Library/Application Support/TradePulse/runtime` 與 `~/Library/Application Support/TradePulse/state`
  - `launchd` 版 runner 會從 internal runtime 啟動，不再直接執行 `Documents` 下的 entrypoint
  - service 專用 `.env` 會自動覆寫 `DATA_ROOT` 到 internal state root，避免卡在外接磁碟權限
  - `start_runner_service()` / `stop_runner_service()` 也改成讀寫 internal service pid / lock / log
- `launchd`
  - LaunchAgent 現在會直接拉起 `run_tradepulse_runner.py`
  - 實測 runner pid 的父程序已變成 `PID 1`，不再是 Codex
  - 手動 kill runner 後，`launchd` 會自動重新拉起新 pid
- 服務行為
  - 即使 Codex 退出，`TradePulse` 仍能由系統接管持續執行
- 網路暫時中斷時，runner 仍會在 loop 中持續重試；若進程真的退出，`launchd KeepAlive` 會再拉起來

## v0.11.25 - External AI daily review

### Why

- 使用者確認可以透過 API key 和其他 AI 對話，希望把這種能力接進 `TradePulse`，但只放在研究 / 檢討層，不直接碰 live 下單
- 目前 daily report 已有 `Strategy Review`、`Loss Attribution`、`Symbol Postmortem`，很適合再接一層外部審稿，方便和 Gemini 等外部模型對照討論
- 需要一個不會污染主交易策略、又能穩定累積外部觀點的第一版落地方式

### What Changed

- `external_ai_review.py`
  - 新增外部 AI 審稿模組
  - 第一版支援 `gemini` provider，透過官方 `generateContent` API 取回結構化 JSON
  - 會輸出：
    - `summary`
    - `strengths`
    - `concerns`
    - `action_items`
    - `verdict`
- `config.py`
  - 新增：
    - `EXTERNAL_AI_REVIEW_ENABLED`
    - `EXTERNAL_AI_REVIEW_PROVIDER`
    - `EXTERNAL_AI_REVIEW_MODEL`
    - `EXTERNAL_AI_REVIEW_API_KEY`
    - `EXTERNAL_AI_REVIEW_TIMEOUT_SECONDS`
- `main.py`
  - 每天中午 daily review 產生後，會額外建立 `external_ai_review-YYYY-MM-DD.json`
  - 若未設定 API key，會安全地標成 disabled，不影響 live
- `reporting.py` / `notion_sync.py`
  - daily report 與 Notion `Daily Review` 新增 `External AI Review` 區塊
- `.env.example` / `README.md`
  - 補上設定與使用方式說明

## v0.11.23 - Noon strategy review debate

### Why

- 使用者希望 `TradePulse` 每天固定時間做一次真正的檢討討論，而不是只根據是否虧損去調整限制
- 既有 `Daily Review` 比較像摘要與建議，還不夠像「strategist / risk / benchmark / execution 互相對照」的複盤會議
- 這份 noon review 需要同時進 daily report 與 Notion，方便和外部 AI 或人工一起對照討論

### What Changed

- `DailyReviewAgent`
  - 輸出新增：
    - `strategist_review`
    - `risk_review`
    - `benchmark_review`
    - `execution_review`
    - `consensus_summary`
    - `action_items`
- `main.py`
  - 每天中午之後即使 Notion daily page 已經發過，也會把當日 `daily_strategy_review-YYYY-MM-DD.json` 存到 service 目錄
  - 本地 daily report 會在 review 寫入後重建，讓同一天的 markdown 報告立即帶出這段複盤
- `reporting.py`
  - daily report 新增 `## Strategy Review`
- `notion_sync.py`
  - Notion `Daily Review` 新增對應的 `Strategy Review` 區塊

## v0.11.22 - Drawdown-aware learning controls

### Why

- `TradePulse` 已經能寫出越來越完整的 postmortem，但 learning loop 仍然太短視：在連續多日低於 `500 USDT` 基準後，控制項仍可能很快恢復成 `fallback_entry_mode=normal`
- benchmark watch 也可能漂去和目前 live 無關的 `BTC/USDT` 候選，讓系統看起來有在觀察外部對照組，實際上卻沒有對齊正在交易的 `SOL/USDT`
- 使用者希望 `TradePulse` 不只是「會檢討」，而是能把最近幾天的失敗真正記住，避免 12 小時就洗白一次

### What Changed

- `config.py`
  - 新增 multi-day learning 參數：
    - `STRATEGY_LEARNING_LOOKBACK_DAYS`
    - `STRATEGY_LEARNING_NEGATIVE_DAY_THRESHOLD`
    - `STRATEGY_LEARNING_RESTORE_POSITIVE_DAYS`
    - `STRATEGY_LEARNING_RESTORE_EQUITY_RECOVERY_RATIO_PCT`
- `main.py`
  - 新增 strategy reflection context builder
  - 會在每次 12h reflection 時帶入：
    - 最近多日 daily PnL
    - current equity vs configured initial capital
    - positive streak
    - 目前 live symbols
    - 目前 live symbol 的 benchmark leader
    - 前一個 slot 的 controls
- `agents.py`
  - `StrategyReflectionAgent` 現在會把 multi-day drawdown 納入 learning controls
  - 若仍處於連續虧損且 equity 未恢復到門檻，`fallback_entry_mode` 會維持 `base_only`
  - 縮短過的 cooldown 也會在恢復條件未達成前被保留
  - `benchmark_watch_candidate` / `benchmark_watch_symbol` 會優先對齊目前 live symbol，而不是漂去不相干的 `BTC/USDT`
- `.env.example` / `README.md`
  - 補上 multi-day learning controls 的設定與說明

## v0.11.21 - Tighter fallback entries and perp margin reserve

### Why

- `4/23` 雖然是獲利日，但 accepted trades 仍然明顯由 `fallback` 主導，代表 live baseline 還是不夠乾淨
- 同一天的 `Available Balance` 幾乎被吃到見底，表示系統雖未超過 2x 上限，仍可能把保證金打得太滿
- `Trade Review` 中的 direction flip 文案容易被誤讀成 executor 漏送 `reduce-only`，這會干擾後續複盤與外部審閱

### What Changed

- `main.py`
  - 新增 `fallback open-entry guard`
  - 當 base strategy 沒有明確同方向訊號時，fallback 若要新開倉，現在必須同時滿足更高門檻：
    - 最低 score
    - 最低 momentum
    - 最低 volume ratio
    - 同方向 trade delta
  - 不符合時，直接轉成 `hold`
- `agents.py`
  - `RiskSupervisorAgent` 新增 `perp_min_available_balance_ratio_pct`
  - 開新倉時若預估可用保證金占總資產比例低於門檻，直接拒絕，避免把可用餘額吃到幾乎歸零
- `reporting.py`
  - `Trade Review` 的 flip 文案改成 reporting-level episode reconstruction，避免再被誤讀成 executor 缺少 `reduce-only`
- `config.py` / `.env.example` / `README.md`
  - 新增 fallback 開倉門檻與 perp 可用保證金留底設定

---

## v0.11.20 - Daily loss attribution for faster postmortems

### Why

- 使用者希望 daily report 能直接帶出「今天到底是哪一層在虧」，方便與其他 AI 一起討論，不要每次都得自己盯盤後手動整理
- 現有的 `Trade Review`、`Symbol Postmortem`、`Decision Attribution` 各自都有價值，但仍缺少一個把：
  - `base / fallback / policy`
  - `long / short`
  - `fees`
  - `benchmark gap`
  直接收束成單一複盤區塊的視角
- 對最近 `500 USDT -> 478 USDT` 的這段回撤來說，這種 loss attribution 應該成為日報標配，而不是額外人工分析

### What Changed

- `reporting.py`
  - 新增 `_build_loss_attribution(...)`
  - 會從當日 accepted trades、trade review episodes、financial snapshot、external benchmarks 自動整理：
    - `Primary Driver`
    - `Realized After Fees`
    - `Accepted by Source`
    - `Losing Episodes by Source`
    - `Losing Episodes by Direction`
    - `Avg Losing Edge by Source`
    - `Benchmark Check`
    - `Worst Episode`
    - `Observations`
  - `build_daily_summary(...)` 新增 `## Loss Attribution` 區塊
- `notion_sync.py`
  - `Daily Review` 現在也會同步顯示精簡版 `Loss Attribution`
- 日報現在更適合直接拿去和其他 AI 討論：
  - 可以快速看出今天是 base strategy、fallback、direction bias，還是 fees/benchmark mismatch 在拖累

---

## v0.11.19 - Reflection controls that actually feed back into live behavior

### Why

- `TradePulse` 已經會寫 daily review / symbol postmortem / trade review，但仍然比較像「會寫檢討報告」，不是「真的會把昨天學到的東西用回今天」
- 使用者明確指出：最近一路從 `500 USDT` 掉到 `478 USDT`，代表框架即使知道自己在虧，也還沒有形成有效的學習閉環
- 尤其最近幾天的 daily attribution 很清楚顯示：
  - `fallback` 常主導 accepted trades
  - `symbol cooldown active` 常是第一大 blocked reason
  這些都應該能直接轉成下一個 12h 視窗的硬控制，而不是只停留在摘要文字

### What Changed

- `models.py` / `strategy_memory.py`
  - `StrategyReflectionSnapshot` 新增 `controls`
  - `strategy_memory.json` 現在會正式保存可執行控制項，而不是只有 summary / biases
- `agents.py`
  - `StrategyReflectionAgent` 現在會輸出並正規化 controls，例如：
    - `fallback_entry_mode`
    - `cooldown_scale`
    - `benchmark_watch_candidate`
    - `benchmark_watch_symbol`
  - fallback reflection 規則新增兩個真正會影響 live 的學習邏輯：
    - 若上一個 12h 視窗是 `fallback` 主導且虧損，下一個視窗改成 `fallback_entry_mode=base_only`
    - 若上一個 12h 視窗大量卡在 `symbol cooldown active`，下一個視窗自動縮短 cooldown
- `main.py`
  - 新增 strategy-memory fallback policy：
    - 當 `fallback_entry_mode=base_only`
    - 且 base strategy 當下為 neutral / hold
    - 就不再允許 fallback 新開方向單
  - `_adaptive_trade_cooldown_seconds(...)` 現在也會讀取 `cooldown_scale`
  - 若目前 slot 的 strategy memory 還沒有 controls，主流程會自動回填一次，不必等下一個 reflection slot 才生效
- `reporting.py` / `notion_sync.py`
  - 日報與 Notion 會顯示：
    - `Learning Controls`
    - `Memory Guard`
  - attribution 也新增 `memory_guard` 類別，方便之後直接看「這次是 base/fallback/policy 還是 memory 在攔」

## v0.11.18 - Decision attribution and neutral-range fallback guard

### Why

- `TradePulse` 最近幾次 intraday 問題，不只是「沒跟上行情」，而是很難分清楚到底是 `base strategy` 做錯，還是外層 fallback override 在干擾
- 尤其在 `current_signal=hold`、`ADX` 偏低的 range / mixed day，系統仍可能被 fallback 持續拉去做空，讓責任歸屬與後續檢討都變得模糊
- 需要把每筆決策的來源寫進 log / 日報，同時在 neutral range 狀態下先擋掉缺乏 follow-through 的 fallback 開倉

### What Changed

- `models.py` / `research.py`
  - `StrategyResearchSnapshot` 新增：
    - `current_signal_type`
    - `current_adx`
    - `current_volume_ratio`
  - 讓主流程不再只從 summary 字串反推 base strategy 當下狀態
- `main.py`
  - 新增 `decision_source` attribution：
    - `base_strategy`
    - `fallback`
    - `fallback_guard`
    - `policy_exit`
  - 每筆候選與最終 selected report 都會寫入 `decision_source`
  - 新增 neutral-range fallback guard：
    - 當 `current_signal=hold`
    - `current_signal_type=hold`
    - `ADX` 低於門檻
    - 且 volume / tape follow-through 不夠強
    時，會把新開方向單轉回 `hold`
- `reporting.py`
  - Daily Summary 新增：
    - `Decision Attribution`
    - `Accepted Attribution`
  - `Latest Decision` 會顯示 `Decision Source`
  - `Symbol Postmortem` 也會納入 attribution 分布與改善提示
- `.env.example` / `config.py`
  - 新增 neutral-range guard 相關設定：
    - `FALLBACK_RANGE_GUARD_ENABLED`
    - `FALLBACK_RANGE_GUARD_ADX_MAX`
    - `FALLBACK_RANGE_GUARD_VOLUME_RATIO`
    - `FALLBACK_RANGE_GUARD_TRADE_DELTA_RATIO`

## v0.11.17 - Adaptive cooldown for single-symbol continuation re-entry

### Why

- `TradePulse` 在單一標的模式下，常常不是完全沒看到機會，而是剛做完一筆就被固定 `900s` cooldown 鎖在場外
- 對 `SOL/USDT` 這種日內有明顯續漲 / 續跌段的盤勢來說，固定長 cooldown 會讓系統錯過後續 continuation 與 re-entry 機會
- 需要把 cooldown 從「固定時間」改成更貼近 intraday 實戰的動態版本：單一幣模式先縮短，強趨勢續抱/續攻時再更短

### What Changed

- `main.py`
  - 新增 `_adaptive_trade_cooldown_seconds(...)`
  - accepted trade 後不再一律套用固定 `TRADE_COOLDOWN_SECONDS`
  - 單一標的模式會先套用較短上限
  - 若同時滿足：
    - `current_signal` 與方向一致
    - momentum 足夠強
    - trade delta 站在同一邊
    - volume ratio 足夠高
    則 cooldown 會進一步縮短，讓續漲 / 續跌段更容易 re-entry
  - `reduce_only` 的平倉單也會採用更短 cooldown，避免剛出場就被長時間鎖死
- `config.py` / `.env.example`
  - 新增 adaptive cooldown 相關設定：
    - `TRADE_COOLDOWN_SINGLE_SYMBOL_CAP_SECONDS`
    - `TRADE_COOLDOWN_TREND_MULTIPLIER`
    - `TRADE_COOLDOWN_MIN_SECONDS`
    - `TRADE_COOLDOWN_REENTRY_MOMENTUM_PCT`
    - `TRADE_COOLDOWN_REENTRY_TRADE_DELTA_RATIO`
    - `TRADE_COOLDOWN_REENTRY_VOLUME_RATIO`
- `README.md`
  - 文件同步新增這組設定，方便單一幣種 focus mode 調校

---

## v0.11.16 - Relax continuation sentiment gate for strong trend follow-through

### Why

- `SOL/USDT` 在 `2026-04-20` 午後出現明顯續漲段時，live 決策其實已經看到 `current_signal=long`，但仍被 fallback strategist 壓回 `hold`
- 問題不在於系統完全沒看到價格或量能，而是 continuation entry 在「輕度負面 sentiment + 強勢 order flow」的情況下仍然太容易被情緒 gate 卡住
- 這會讓框架在明顯續漲或續跌日的中後段，出現「知道方向，但不願意重新上車」的遲鈍行為

### What Changed

- `agents.py`
  - 對 `current_signal == long/short` 的 continuation entry 新增更明確的強趨勢條件
  - 當 momentum 與 order flow 都明顯站在同一邊時，允許 continuation setup 容忍較輕微的反向 sentiment
  - 讓系統在強續漲 / 強續跌段更願意跟單，而不是無條件被 `fear` 或輕度反向情緒壓回 `hold`
- `main.py`
  - `strategy_research` payload 現在會把 `current_signal` 一起寫進 decision/trade logs
  - 之後做 daily postmortem 或單筆回放時，可以直接看出「研究層當時到底看到 long / short / hold」，不用再從 summary 字串反推

---

## v0.11.14 - Raise demo position budget to 40%

### Why

- 目前帳戶資金規模下，`MAX_POSITION_PCT=0.20` 雖然安全，但對 `SOL` 這類較高單價 perp 太保守，常讓有效訊號在 sizing 後落到交易所最小可執行單以下
- 使用者希望系統更接近「以可用資金積極參與」的訓練模式，而不是每筆單都只拿很小一部分去試單
- 直接改成 `100%` 會太接近常態 all-in，不符合我們目前仍需保留風險緩衝、手續費空間與多標的彈性的設計

### What Changed

- `.env`
  - 將執行中的 `MAX_POSITION_PCT` 從 `0.20` 提高到 `0.40`
- `.env.example` / `config.py` / `README.md`
  - 將預設與文件同步更新為 `0.40`
- `scripts/run_trading_supervisor.sh`
  - 修正 supervisor 不再因為 runner 的非零退出碼一起退出
  - 讓新版倉位設定能穩定套用到背景服務，而不是只改到檔案
- 新版 runner 重啟後，單筆風險預算會改為更接近：
  - `available balance × BUY_BALANCE_BUFFER_PCT × 0.40`
  - 在目前 `BUY_BALANCE_BUFFER_PCT=0.95` 下，等於大約使用可用資金的 `38%`

---

## v0.11.15 - SOL-first focus mode and daily symbol postmortem

### Why

- 使用者希望先把 live 注意力集中在單一幣種，避免多標的同時觀察造成資金與檢討焦點分散
- 現有 daily review 雖然有總結，但還缺少「像人工回顧 SOL 一整天走勢那樣」的單一標的 postmortem
- 需要讓框架每天自動指出：某個幣今天到底是趨勢日、震盪日，系統是因為 `hold` 太多、cooldown 太長，還是 sizing / fee hurdle 卡住

### What Changed

- `reporting.py`
  - 新增 `symbol_postmortem`
  - 會自動為單一重點標的整理：
    - 價格由開頭到結尾的漲跌
    - 日內區間
    - `buy / sell / hold`
    - approved / accepted / rejected
    - 該標的自己的 blocked / rejected 主因
    - 對應的改善方向
- `agents.py`
  - `DailyReviewAgent` fallback 現在會把 symbol postmortem 摘要與改善方向納入每日結論
- `notion_sync.py`
  - `Daily Review` 新增 `Symbol Postmortem` 區塊
- 本機執行環境
  - 目前先收斂成 `SOL/USDT` 單一 observation pool，方便做日內 attribution 與策略檢討

---

## v0.11.13 - Continuation entries and executable minimum order sizing

### Why

- `SOL/USDT` 這類高單價 perp 在目前資金規模下，經常出現「risk 通過，但 quantity rounds to zero」的拒單，代表風控看到的最小單額仍停留在理論值，沒有反映交易所真正的 `qtyStep + minQty`
- live 主策略原本比較偏第一個 breakout 進場，對「已經走出趨勢、但仍在延續」的盤勢參與度不夠，容易在明顯弱勢日中後段變成長時間 `hold`

### What Changed

- `exchange.py`
  - 新增 lot-size 約束抽象，統一解析：
    - `qtyStep`
    - `minOrderQty`
    - `minOrderAmt / minNotionalValue`
  - 新增 `executable_min_order_value_usdt(...)`
  - 風控與下單驗證不再只看理論 `5 USDT`，而會用「以當前價格換算後，真正能下得出去的最小 notional」
- `main.py`
  - 候選標的的 `min_order_value_usdt` 改為使用 executable minimum
- `agents.py`
  - `RiskSupervisorAgent` 會用 executable minimum 做 sizing 判斷
  - demo aggressive starter-size 現在同時支援開多與開空，不再只偏向 `buy`
  - `StrategistAgent` fallback 現在會認 `current_signal`，讓趨勢延續訊號能更容易轉成實際 `buy/sell`
- `backtest.py`
  - `donchian_adx_signal(...)` 新增 continuation long/short 條件
  - 不再只認第一個 breakout，ADX 強且均線/DI 結構仍延續時，可以再次給出方向訊號
- `research.py` / `models.py`
  - `StrategyResearchSnapshot` 新增 `current_signal`
  - 讓 strategist 能直接吃到外部主策略當下是 `long / short / hold`

---

## v0.11.11 - Order-flow / microstructure features in live decision path

### Why

- 先前的 live decision 幾乎只看 15m K 線、volume、sentiment、replay/backtest，還不是真正會「看盤面」的短線系統
- 雖然專案目標是 `USDT perpetual intraday long/short`，但缺少 `spread / depth / trade delta / large prints` 這層資訊，會讓 strategist 對短線進出場的判斷過於鈍化
- 需要把 order book 與 recent public trades 轉成 deterministic 特徵，接到主資料模型，而不是只停留在抽象討論

### What Changed

- `models.py`
  - `MarketSnapshot` 新增 microstructure 欄位，例如：
    - `spread_bps`
    - `top_book_imbalance`
    - `depth_imbalance`
    - `bid/ask wall notional`
    - `trade_delta_ratio`
    - `large_buy_count / large_sell_count`
- `exchange.py`
  - `BybitDemoExchangeClient` / `BybitDemoPerpExchangeClient` 現在在 full snapshot 會額外抓：
    - `/v5/market/orderbook`
    - `/v5/market/recent-trade`
  - 並把原始資料壓成可決策的 microstructure 特徵
  - `set_position_protection()` 現在會把 Bybit 的 `not modified` 視為 `unchanged`，避免 protection sync 把整輪 cycle 打成 error
  - `MockExchangeClient` 也補上合成盤面特徵，方便本地 smoke test
- `agents.py`
  - 新增 `OrderFlowCollectorAgent`
  - `StrategistAgent` prompt 與 fallback decision 現在會吃：
    - order flow summary
    - depth imbalance
    - trade delta
    - large prints
- `main.py`
  - `market summary` 現在會附帶 order-flow 摘要
  - `LLM wake gate` 新增：
    - `depth imbalance`
    - `trade delta`
    - `large prints`
- `runner.py`
  - monitor loop 仍維持輕量價格監控
  - `include_microstructure=False`，避免把 order book / trades 每 30 秒無腦重抓，讓高頻監控延遲失控
- `config.py` / `.env.example`
  - 新增 microstructure 相關設定：
    - `MARKET_MICROSTRUCTURE_ENABLED`
    - `ORDERBOOK_DEPTH_LIMIT`
    - `RECENT_PUBLIC_TRADE_LIMIT`
    - `MICROSTRUCTURE_CACHE_TTL_SECONDS`
    - `LLM_WAKE_DEPTH_IMBALANCE`
    - `LLM_WAKE_TRADE_DELTA_RATIO`
    - `LLM_WAKE_LARGE_TRADE_COUNT`

---

## v0.11.12 - Normalize repeated Why Blocked reasons

### Why

- Daily Summary 的 `Why Blocked` 原本會把帶有動態數字的原因逐條列出，例如不同秒數的 `symbol cooldown active: XXs remaining`
- 這會讓同一種阻擋原因在日報裡重複很多次，降低可讀性，也讓 Notion / UI 的 top blocked reason 比較不準

### What Changed

- `reporting.py`
  - `_normalize_blocked_reason(...)` 現在會把常見動態原因折疊成固定類別，例如：
    - `symbol cooldown active`
    - `expected edge below fee hurdle`
    - `fast-cycle confidence too low`
    - `no base asset available to sell`
  - Daily Summary / Notion / UI 會共用這份整理後的 blocked reason counts
- 這讓 `Why Blocked` 變成真正的聚合計數，而不是同樣原因被不同數字拆成很多列

---

## v0.11.10 - Mode-scoped daily reporting and equity curve isolation

### Why

- `2026-04-17` 的日報曾出現不合理的超大資產數字，根因不是交易真的異常，而是 daily summary 把不同 `TRADING_MODE` 的 log 混在一起統計
- 同樣的問題也污染了 equity curve，讓 `bybit-demo-perp` 的資金曲線被 `mock` 測試資料拉歪
- 既然框架同時支援 `mock`、`bybit-demo`、`bybit-demo-perp`，報表與資產曲線就必須明確按 mode 隔離，否則 Daily Review 會失去可信度

### What Changed

- `reporting.py`
  - `load_daily_summary_data(...)` 現在會依照 `settings.trading_mode` 過濾 daily records 與 all records
  - Daily Summary 會明確顯示 `Mode`
- `storage.py`
  - 新增 `mode_scoped_path(...)`
  - 讓 equity curve history / chart 依 mode 生成獨立檔名
- `main.py`
  - reporting finalize 階段改用 mode-scoped equity history 與 chart 路徑
- Daily report 與 equity curve 現在會分開維護，例如：
  - `equity_curve_history-bybit-demo-perp.jsonl`
  - `equity-curve-latest-bybit-demo-perp.svg`
- 修正後，`bybit-demo-perp` 的日報資產快照已回到正常區間，不再被 `mock` 價格資料污染

---

## v0.11.9 - External benchmark pipeline for Grid / Alpha Arena / public candidates

### Why

- 雖然 live 主策略已經 reset 成單一公開規則策略，但 `Grid`、`Alpha Arena`、以及其他可信外部做法如果只停留在文件與討論層，仍然無法真正幫框架學習
- 需要一條 research-only 的 benchmark 管線，把外部候選策略先做 replay / benchmark，再把結果餵回日報、Notion 與 12h reflection，而不是直接塞進 live trading
- 這樣才能同時滿足兩件事：
  - live 主線保持收斂
  - 外部策略持續被回測、被比較、被累積成研究資料

### What Changed

- 新增 `config/external_benchmark_library.json`
  - 明確定義目前 research-only 的外部 benchmark 候選：
    - `donchian_adx_perp_v1`
    - `grid_range_reversion_v1`
    - `bollinger_rsi_mean_reversion_v1`
    - `alpha_arena_public_imports`
- 新增 `trading_agents/external_benchmarks.py`
  - 統一處理外部 benchmark 候選的：
    - signal generation
    - replay / benchmark
    - Alpha Arena normalized dataset 載入
    - 最新 benchmark snapshot 持久化
- 新增 `scripts/run_external_strategy_benchmarks.py`
  - 可手動強制跑一輪外部 benchmark
- `StorageLayout` 新增：
  - `data/external_benchmarks/normalized`
  - `reports/benchmarks`
  - `service/external_benchmark_latest.json`
- `main.py` 現在會低頻刷新外部 benchmark 快照（預設每 4 小時）
  - 只進 research/reporting，不碰 live executor
- `reporting.py` / `notion_sync.py` / `DailyReviewAgent`
  - 日報、Notion、daily review 現在會顯示：
    - top benchmark candidate
    - top Alpha Arena candidate
    - symbol-level benchmark leader
  - 12h strategy reflection 也會把 benchmark leader 納入 bias / summary 參考
- `README.md` 與策略真相文件補上目前 external benchmark 的定位與使用方式

---

## v0.11.8 - External strategy reset to Donchian + ADX

### Why

- 過去的 live trading 核心仍然建立在內部 hand-crafted 策略池上，與「採用更有公信力、可審計策略來源」這個目標沒有真正對齊
- 雖然基礎設施、風控、Notion、報表與 perp 執行層已經成熟，但核心 alpha 來源仍不夠可信
- 需要把 live 主策略收斂成一條公開、經典、可回測、適合多空 perp 的規則策略，避免系統繼續停留在四不像的混合狀態

### What Changed

- 移除原本作為 live 主策略池的內部 intraday 三策略：
  - `intraday_breakout_perp_v1`
  - `intraday_pullback_perp_v1`
  - `intraday_reversal_scalp_v1`
- `strategy_library.json` 改為單一主策略：
  - `donchian_adx_perp_v1`
- `MarketSnapshot` 新增：
  - `opens`
  - `highs`
  - `lows`
- `exchange.py` 現在會把 Bybit / mock / Binance 的 OHLC 一起帶入 snapshot
- `backtest.py` 重寫為：
  - Donchian breakout + Wilder ADX 過濾
  - ATR 風格動態 TP / SL replay
- `research.py` 重寫為：
  - 單一外部主策略模式
  - 不再讓內部策略池互相競爭
  - 研究摘要會明確輸出 `current_signal`
- 新增當前策略真相文件：
  - `CURRENT_LIVE_STRATEGY_TRUTH_TABLE.md`

---

## v0.11.7 - Daily Review-only Notion equity chart

### Why

- 折線圖更適合作為日報級別的資產曲線，而不是跟著 `heartbeat` 持續重畫的即時卡片
- 把圖表塞進 `Live Status` 會增加 Notion 同步成本，也更容易讓頁面更新與檔案上傳互相干擾
- 最穩的做法是：
  - `Live Status` 保持輕量、文字化
  - `Daily Review` 才承載真正的 equity chart 圖片

### What Changed

- Notion 圖表策略調整成 `daily-only`
- `Live Status` / heartbeat 頁面不再嘗試插入 equity chart 圖片
- `Daily Review` 生成時會：
  - 讀取本地 `equity-curve-latest.svg`
  - 透過 Notion file upload API 上傳圖檔
  - 把圖表插入 `Daily Review` 頁面內容
- 同步程式改成：
  - 一般頁面內容操作仍走舊版 Notion block API
  - 圖檔上傳與 file-upload image block 走新版 API
  - 降低整體相容性風險

---

## v0.11.6 - Equity curve tracking and chart output

### Why

- 只看單日 PnL 與文字摘要，還是不夠直觀，很難快速判斷資金曲線是穩定上升、持平震盪、還是持續回撤
- 需要一條可持續更新的資金折線圖，讓你不開 UI 也能透過 Notion / 報表掌握整體表現
- 這條曲線也會成為後續 benchmark、drawdown、風控優化的重要基礎資料

### What Changed

- 新增 equity history 持久化：
  - `service/equity_curve_history.jsonl`
- 新增最新資金曲線圖輸出：
  - `reports/charts/equity-curve-latest.svg`
- reporting 流程現在每輪會：
  - 讀取最新 financial snapshot
  - 追加 equity point
  - 重畫 SVG 折線圖
- Notion Live Status / Daily Review 新增：
  - `Equity Curve` sparkline
  - 最近區間 min / max 資產範圍
- README 補上 equity curve 的用途與檔案位置說明

---

## v0.11.5 - Alpha Arena signal import and benchmark backtest

### Why

- `Alpha Arena` 對目前專案最有價值的第一步，不是直接當交易訊號，而是先當公開 benchmark / research source
- 如果公開內容包含 signals / trades / reasoning，我們需要先有一條乾淨的路徑把它匯入、標準化，再拿來做 replay 與比較
- 這樣才能逐步回答：
  - 外部強模型和我們的方向是否一致
  - 它們的節奏是否更像理想的 intraday long/short
  - 哪些行為值得餵回 strategist / evaluator

### What Changed

- 新增 `trading_agents/alpha_arena.py`
  - 標準化公開 signal records
  - 匯出 normalized `jsonl`
  - 用 Bybit 公開 K 線做基礎 benchmark replay
- 新增 `scripts/alpha_arena_import_and_backtest.py`
  - 可直接把一份公開 Alpha Arena / 類 Alpha Arena JSON 匯出轉成 benchmark 報表
- `StorageLayout` 新增：
  - `data/alpha_arena/raw`
  - `data/alpha_arena/normalized`
- README 補上第一階段 Alpha Arena benchmark 用法
- 這一版仍維持 research-only，不會把 Alpha Arena 公開訊號直接接入 live executor

---

## v0.11.4 - Intraday-first strategy pool and adaptive hold policy

### Why

- 原始產品目標是「幣圈 USDT perpetual 的日內做多做空當沖」，但舊版策略池比較像短週期方向交易，沒有明確被約束成 intraday-first
- 舊版 backtest 主要只看下一根 K 的報酬，較像驗證方向對錯，而不是驗證完整日內交易生命週期
- 單純用「超過時間就強制平倉」也太死板，所以新的 intraday 政策需要同時滿足：
  - 偏日內
  - 避免卡單
  - 但趨勢還在時允許延長持有

### What Changed

- 策略池改成明確的 intraday 版本：
  - `intraday_breakout_perp_v1`
  - `intraday_pullback_perp_v1`
  - `intraday_reversal_scalp_v1`
- `backtest.py` 不再只看下一根 K，改成用短持有窗 + TP/SL/time-stop 方式回放 intraday setup
- `research.py` 的策略比較已對齊新的 intraday 策略池與 replay 邏輯
- 新增本地 position policy state，追蹤：
  - 持倉開始時間
  - 持有分鐘數
  - 持有 bars 數
- `main.py` 新增 adaptive intraday exit policy：
  - stagnation exit
  - overheld-without-edge exit
  - optional end-of-day de-risk / flatten
- 這套 policy 不是硬性一翻兩瞪眼：
  - 若趨勢仍延續且保護單已跟上，允許繼續抱
  - 只有優勢鈍化、卡住、或接近日切又沒有足夠 edge，才會主動出場
- `.env.example` 與 README 新增 intraday policy 相關設定

---

## v0.11.3 - Alpha Arena integration plan

### Why

- 專案已經進入 perp + 多空 + safety rails 階段，下一步不只需要持續修策略，也需要一個外部 benchmark 來檢查我們的方向判斷、持倉節奏與 exit 品質
- `Alpha Arena` 類型的公開 AI 交易競賽資料，適合作為 research 與 evaluator 的參考，但如果直接拿來當 live 訊號，會和現有交易所、成本與風控語意產生落差
- 需要先把「如何正確接入」寫清楚，避免後續把 benchmark、strategy、grid、live execution 混成一團

### What Changed

- 新增 `ALPHA_ARENA_INTEGRATION_PLAN.md`
- 明確定義 Alpha Arena 在目前架構中的定位：
  - benchmark
  - research source
  - evaluator reference
  - regime calibration input
- 明確說明它與未來 `Grid / OscillationDetector` 並不衝突，而是不同層次的能力
- 增加分階段導入建議：
  - offline dataset
  - research-only integration
  - regime calibration
  - soft influence on live decisions
- README 與 SYSTEM_ARCHITECTURE 補上入口與架構定位說明

---

## v0.11.2 - Perp profit lock ladder

### Why

- `v0.11.1` 已經能在 Bybit Demo perp 上自動掛交易所 TP/SL，但保護單仍偏靜態，無法在部位開始浮盈後主動把停損往 breakeven 或獲利區移動
- 4/15 到 4/16 的報表討論提醒我們，單純有硬停損還不夠，還需要一層「贏家不能輕易變輸家」的鎖利機制
- 既然現有架構已經能讀取 entry / mark / current TP/SL，最合理的下一步就是把 profit lock 做成輕量、規則式、每輪可自動同步的 safety rail

### What Changed

- 新增 perp 浮盈鎖利階梯：
  - `PERP_PROFIT_LOCK_TRIGGER_PCT`
  - `PERP_PROFIT_LOCK_BREAKEVEN_OFFSET_PCT`
  - `PERP_PROFIT_LOCK_TRIGGER_2_PCT`
  - `PERP_PROFIT_LOCK_STOP_2_PCT`
- `main.py` 會根據目前倉位的浮盈百分比，動態重算保護目標：
  - 第一階段達標後，把 stop loss 往 breakeven 附近推進
  - 第二階段達標後，把 stop loss 推進到更明確的 in-profit 區域
- 既有 perp 倉位不必等到重新下單才更新保護；每輪 candidate 掃描都會先同步一次交易所保護單
- 保護同步現在具備 `unchanged` 快速路徑：若交易所上的 TP/SL 已經符合目標，就不重複送 API
- `.env.example` 與 `README.md` 已補上 profit lock ladder 的設定說明

---

## v0.11.1 - Perp safety rails and reporting correction

### Why

- `v0.11.0` 已經讓系統能在 Bybit Demo perp 上開多與開空，但合約版仍缺少真正的安全底座
- 4/13 報表與實際檢查顯示，系統還沒有槓桿上限、強平距離防護與交易所硬停損/止盈保護，這會讓後續要做的 regime / grid 升級缺少安全邊界
- perp 報表也仍沿用部分現貨語意，像 `Available USDT (-126.5%)` 這種顯示會誤導判讀

### What Changed

- `AccountState` 新增 perp 專用風控欄位：
  - `leverage`
  - `liq_price`
  - `position_im_usdt`
  - `position_mm_usdt`
  - `take_profit_price`
  - `stop_loss_price`
  - `trailing_stop_distance`
  - `position_status`
  - `is_reduce_only`
- Bybit perp client 新增：
  - `set_leverage()`
  - `set_position_protection()`
- 新開 perp 倉位成功後，會自動嘗試設定交易所保護單：
  - hard stop loss
  - take profit
  - optional trailing stop
- `risk_supervisor` 新增合約專用檢查：
  - projected leverage 上限
  - liquidation buffer 太近時拒絕加碼
  - maintenance margin 過高時警告
- Daily report / human report / Notion live status 改為正確的 perp 語意：
  - `Available Balance`
  - `Gross Exposure`
  - `Effective Leverage`
  - `Liq buffer`
  - `TP / SL`
- `.env.example` 與 `README.md` 新增 perp 安全底座參數：
  - `PERP_MAX_LEVERAGE`
  - `PERP_MIN_LIQUIDATION_BUFFER_PCT`
  - `PERP_HARD_STOP_LOSS_PCT`
  - `PERP_TAKE_PROFIT_PCT`
  - `PERP_TRAILING_STOP_PCT`
  - `PERP_ENABLE_PROTECTION_ORDERS`

---

## v0.11.0 - Bybit perp prototype and directional reporting

### Why

- 原始專案目標不是只有現貨短線，而是希望在高波動的幣圈市場同時具備做多與做空能力
- 近半個月的回顧顯示，系統不只 long entry 品質仍待改善，也明顯受限於 spot-only 架構，無法在下跌行情中參與獲利
- 既然 runner、reporting、Notion、service layer 都已經穩定，下一步就應該把原型正式往 `perp long/short` 推進，並把多空拆分統計納入報表

### What Changed

- 新增 `bybit-demo-perp` 交易模式，接入 Bybit Demo USDT perpetual / linear 交易路徑
- `AccountState` 擴充為支援合約語意，新增：
  - `market_type`
  - `position_side`
  - `net_position`
  - `entry_price`
  - `mark_price`
  - `position_notional_usdt`
  - `unrealized_pnl_usdt`
  - `cum_realized_pnl_usdt`
  - `total_equity_usdt`
  - `available_balance_usdt`
- `strategist`、`risk_supervisor`、`executor` 全部理解 perp 語意：
  - `buy` 可代表開多或回補空單
  - `sell` 可代表開空或平掉多單
  - 平倉時會正確使用 `reduceOnly`
- 修正 cooldown key，改為以 `mode:symbol` 區分，避免舊 spot 冷卻狀態污染 perp 模式
- Daily report、Live Status、Notion 狀態頁新增方向性統計：
  - `Realized PnL Split`
  - `Directional Exposure`
  - `Long vs Short`
  - `long / short proposals`
  - `long / short accepted`
- 日報重建後會直接顯示目前多空曝險與方向別損益，讓隔天開始的 daily report 能直接觀察 perp 版本表現
- `.env.example` 與 `README.md` 補上 `bybit-demo-perp` 的設定與使用說明

---

## v0.10.0 - LLM wake gate and early-exit filtering

### Why

- 4/10 的討論確認 full cycle 最大壓力不是市場資料抓取，而是本地 LLM 在多標的流程中被過度喚醒
- 即使 sentiment cache 與 latency breakdown 已經降低外部資料成本，系統仍需要在喚醒 strategist / risk LLM 前做一層輕量判斷
- 需要避免「安靜盤勢」也耗費完整 LLM 推理，同時又不能漏掉已有持倉時的出場與風控訊號

### What Changed

- 新增 `wake_score` early-exit 流程，每個候選標的會先用輕量 market/account 訊號判斷是否值得喚醒 LLM
- `wake_score` 目前使用：
  - recent volatility
  - MA5 / MA20 momentum spread
  - recent volume expansion
  - 20-candle high / low breakout proximity
  - held-position price move
- 空倉標的預設 `wake_score >= 2` 才喚醒 LLM
- 有持倉標的預設 `wake_score >= 1` 就可喚醒 LLM，避免錯過退場或風控判斷
- 沒達標的候選仍會保留 market / sentiment / backtest / deterministic strategy 結果，但不花本地 LLM 推理成本
- Daily report、Web UI、Notion Live Status / Daily Review 新增 `LLM Wake Rate`
- 新增 wake gate 設定：
  - `LLM_WAKE_GATE_ENABLED`
  - `LLM_WAKE_MIN_SCORE`
  - `LLM_WAKE_POSITION_MIN_SCORE`
  - `LLM_WAKE_VOLATILITY_PCT`
  - `LLM_WAKE_MOMENTUM_PCT`
  - `LLM_WAKE_VOLUME_RATIO`
  - `LLM_WAKE_BREAKOUT_PROXIMITY_PCT`
  - `LLM_WAKE_POSITION_MOVE_PCT`
- 修正 wake rate 報表口徑：舊紀錄若沒有 `llm_wake` 欄位，不再被誤算成「未喚醒」

---

## v0.9.0 - Latency instrumentation and sentiment caching

### Why

- 4/9 的報表已經明確暴露出決策延遲重新升高，但原本只有單一 `Avg Decision Latency`，很難知道瓶頸卡在哪一段
- full cycle 內每個標的都做完整辯論，讓本地 LLM 在多標的模式下容易把整輪耗時拉長
- sentiment 外部資料在同一輪與重啟後都可能重抓相同來源，造成不必要的網路等待
- dust 部位雖然不致命，但會持續污染決策上下文，讓策略與風控浪費注意力在不可操作倉位上

### What Changed

- 新增 stage-level latency metrics，將延遲拆成：
  - `Market`
  - `Sentiment`
  - `Backtest`
  - `Research`
  - `Strategist`
  - `Risk`
  - `Selector`
  - `Executor`
  - `Evaluator`
- Daily report、Web UI、Notion Live Status / Daily Review 全部新增：
  - `Latency Breakdown Avg`
  - `Latency Breakdown P95`
- full cycle 的重型 LLM 辯論改為「先跑完整個觀察池，再只對 selector 最後選中的候選做 selected-candidate debate」
- 新增 `LLM_SELECTED_CANDIDATE_ONLY` 設定，預設減少每輪不必要的多標的辯論成本
- 新增 dust normalization：低於交易所最小下單額的殘餘部位會保留在帳戶資料中，但不再作為可執行持倉參與決策
- sentiment 加入兩層快取：
  - 同進程 TTL 快取
  - 跨重啟檔案快取 `sentiment_http_cache.json`
- 新增：
  - `SENTIMENT_REQUEST_TIMEOUT_SECONDS`
  - `SENTIMENT_CACHE_TTL_SECONDS`
  - `DUST_POSITION_MULTIPLIER`
- Daily Review 的改善建議收斂，不再直接鼓勵因資金利用率低就放寬主策略進場門檻

---

## v0.8.0 - GitHub-ready portable repo

### Why

- 專案已經從本機實驗腳本，變成可持續維護的框架
- 需要能在其他電腦上 `clone` 後快速部署
- 需要把文件與啟動方式從「只適合原開發機」整理成「可交接、可重建」

### What Changed

- 初始化 Git repository 並推到 GitHub
- 新增獨立 `CHANGELOG.md`
- 重寫 `README.md`，改成部署與操作導向
- 補 `scripts/setup_local_env.sh`，讓 clone 後可快速建立環境
- 將多個啟動腳本改為相對路徑，不再綁死本機目錄
- 調整 `.gitignore`，避免把 `.env`、runtime 與 log 一起提交
- 把 `DATA_ROOT` 預設收斂成 `./runtime`

---

## v0.7.0 - Service hardening and operations visibility

### Why

- 系統曾發生 runner 停掉但沒有立即被發現的狀況
- Daily report 缺失時，不容易快速判斷是策略沒觸發，還是背景服務早已停止
- UI 需要直接呈現 service health，而不是只能靠終端機檢查 PID

### What Changed

- 將 runner 從脆弱的單次背景啟動改為 `supervisor + runner` 結構
- 補 `runner_supervisor.pid`、`runner.pid`、對應 log 與 stale lock 清理
- Web UI 顯示 `Supervisor PID / Runner PID / Uptime`
- 修正 `2026-04-05` 無日報時的補報與服務重啟流程
- 強化 background service 與 UI 解耦，UI 關掉不影響 runner 持續運作

---

## v0.6.0 - Financial reporting and review system

### Why

- 只看 decision / blocked / executed 數量，無法回答「到底有沒有賺錢」
- 需要把系統從工程 debug log，升級成交易績效報表
- 需要把每日回顧與即時狀態分開，避免 Live Status 和 Daily Review 混在一起

### What Changed

- Daily report 增加 `Financial Snapshot`
- 新增：
  - `Total Portfolio Value`
  - `Daily PnL`
  - `Realized / Unrealized PnL`
  - `Daily / Cumulative Fees`
  - `Capital Utilization`
- Daily Review 改成台灣時間中午後發布一次，不再被小 cycle 持續覆蓋
- Live Status 改回精簡版，專注於即時監控資訊
- Daily report / UI / Notion 的營運資訊與財務資訊同步對齊

---

## v0.5.0 - Debate workflow and 12-hour strategy memory

### Why

- 單純的 pipeline 決策還不夠接近「多個 AI 互相質疑與收斂」的原始目標
- 若每個大 cycle 都立即調整策略，容易過度擬合短線波動
- 需要有節制地保留反思結果，讓 agent 有一致的中期記憶

### What Changed

- `strategy_researcher`、`strategist`、`risk_supervisor`、`selector` 增加辯論式流程
- 新增 `risk_feedback` 與決策辯論紀錄
- 新增 `strategy_memory.json`
- 策略反思改為每 `12` 小時更新一次，而不是每輪都改
- Web UI / Notion / report 顯示最新辯論內容與 strategy memory slot

---

## v0.4.0 - Expectancy-driven aggressive demo mode

### Why

- 早期策略過於保守，幾乎只會 `hold`，不利於在 demo 環境中訓練系統
- 單看勝率不夠，應該要納入期望值與盈虧比
- 使用者希望在模擬盤中更重視「整體營收為正」而不是單次虧損次數

### What Changed

- 回測與策略研究新增：
  - `avg_win_pct`
  - `avg_loss_pct`
  - `expectancy_pct`
  - `profit_factor`
- `strategist` / `risk_supervisor` 開始使用 expectancy 與 reward/risk 作為核心依據
- 加入 `DEMO_AGGRESSIVE_MODE`
- 放寬 demo 模式風控，使其更適合訓練與策略探索
- 微調 `buy` / `hold` / `sell` 判準，降低完全不出手的情況

---

## v0.3.0 - Continuous monitoring and execution-aware control

### Why

- 原本較像「每 15 分鐘批次重跑」的系統，不符合持續監控市場的設想
- 需要把大 cycle 與小 cycle 的任務分開
- 需要讓系統更偏向可執行訊號，而不是只挑分數最高但做不了的候選

### What Changed

- runner 改成 `30s` 監控輪詢 + 條件觸發 full cycle
- 新增：
  - `MONITOR_INTERVAL_SECONDS`
  - `RUN_INTERVAL_SECONDS`
  - `PRICE_TRIGGER_PCT`
  - `MICRO_CYCLE_TRIGGER_PCT`
  - `POSITION_MICRO_TRIGGER_PCT`
- 盤中 monitor 不再只是 idle watching，會因價格與持倉變化觸發決策
- `selector` 更偏向可執行候選
- UI 明確區分 continuous mode 與 debug control

---

## v0.2.0 - Multi-symbol flow, reporting clarity, and execution safety

### Why

- 早期流程以單標的視角為主，不夠貼近多標的觀察池設計
- 報表曾出現 UTC/台北日期混亂與舊 runner 覆寫問題
- 系統常產生「無持倉卻賣出」或「小於交易所最小單額」的不可執行提案

### What Changed

- Web UI 改為顯示多標的觀察池流程
- 新增完整流程階段可視化：
  - Setup
  - Market
  - Sentiment
  - Backtest
  - Research
  - Strategist
  - Risk
  - Selector
  - Executor
  - Evaluator
  - Reporting
- 修正 Daily Summary 時區與統計欄位重疊問題
- 新增 `Why Blocked` 統計
- 新增交易所最小單額檢查，避免把不可成交小單送出
- 修正 `sell` 在沒有 base asset 或倉位太小時的處理

---

## v0.1.0 - Initial local multi-agent trading MVP

### Why

- 需要一個本地 AI agent 原型，先在模擬資金環境驗證整套流程
- 目標是先打通資料流、代理分工、風控與執行，不急著直接上實盤

### What Changed

- 建立多代理框架
- 初步角色包括：
  - `market_collector`
  - `sentiment_collector`
  - `strategist`
  - `risk_supervisor`
  - `executor`
  - `post_trade_evaluator`
- 接上 `Ollama` 本地模型
- 支援 `mock` 與 `Bybit Demo`
- 建立市場資料、情緒資料、交易 log、日報與簡易 Web UI 的基本骨架

---

## Next Documentation Step

後續建議每次版本更新都維持這個格式：

```md
## vX.Y.Z - Title

### Why
- 為什麼要改

### What Changed
- 改了哪些行為、架構、設定、文件
```

如果需要，我可以下一步再幫你補：

- GitHub `Releases` 用的 release notes 模板
- Notion 版的同內容版本紀錄頁
- 每次 release 前自動提醒更新 changelog 的流程
# v0.11.22 - Clarify daily PnL basis and align capital baseline

Why:
- Daily reports showed `Daily PnL` without stating what it was being compared against, which made the number hard to interpret.
- The configured initial capital displayed in reports was still `150 USDT` even after the demo account baseline had been raised to `500 USDT`.

What Changed:
- Added explicit `Daily PnL Basis` fields to the financial snapshot, using the first portfolio snapshot of the Taiwan trading date.
- Daily reports and Notion pages now show both `Configured Initial` and `Daily PnL Basis`.
- Updated active/runtime and example settings to use `INITIAL_BALANCE_USDT=500`.
# v0.11.23 - Tighten wake gate and add PnL bridge diagnostics

Why:
- `TradePulse` still woke the LLM almost every cycle, even on quiet range days, which kept latency too high.
- Daily reports still required manual interpretation to understand how `Daily PnL` related to realized and unrealized movement.
- `policy_exit` appeared in attribution counts, but it was still hard to tell whether it was driving behavior or mostly showing up as metadata.

What Changed:
- Tightened default wake thresholds and added a Python-side quiet-market / order-flow-only short-circuit for no-position states.
- Added `PnL Bridge` fields to the financial snapshot and surfaced them in daily reports and Notion.
- Added `Policy Exit Diagnostics` to daily reporting so policy-driven exits can be reviewed separately from base/fallback entries.

# v0.11.25 - Sharpen no-trade attribution and tighten wake gate again

Why:
- On flat no-trade days, `Loss Attribution` could still describe the outcome as execution drag, which was misleading.
- `TradePulse` was still waking the LLM too often on quiet, no-position sessions, despite producing no trade proposals.

What Changed:
- Zero-accepted-trade days now report a clearer primary driver such as `no-trade day; no validated edge passed entry filters`.
- Added an explicit observation for observe-only sessions so daily postmortems line up with actual execution.
- Tightened default wake thresholds again and added a `weak-setup short-circuit` for no-position states with only weak core signals away from range edges.

# v0.11.26 - Cap same-episode add-ons to reduce fee drag

Why:
- `TradePulse` could slice one directional episode into too many accepted entries, which looked directionally correct but left too much of the edge to fees.
- Position hold tracking also reset too easily during same-direction scaling, which made intraday hold policy less trustworthy.

What Changed:
- Added `INTRADAY_MAX_ENTRIES_PER_EPISODE` so the live executor can block same-direction add-ons after a configurable number of fills.
- Position policy state now preserves the same episode across same-direction scaling and tracks `entry_count`.
- Candidate snapshots now expose `entry_count` and `max_entries_per_episode` for easier postmortem and daily review analysis.

# v0.11.27 - Make reports explain positions and executed trades

Why:
- Daily and 5-minute reports still compressed current positions into a single dense line, which made it hard to answer basic questions like "is this long or short?", "when did we enter?", and "where are TP/SL?".
- Daily review also lacked a clean, timestamped trade ledger for the current Taiwan date, so manual replay was still too dependent on raw logs.

What Changed:
- Current perp positions now show direction, opened time, entry price, mark price, TP/SL, leverage, liquidation buffer, and entry count in a multi-line readable format.
- Daily reports now include an `Executed Trades Today` section with timestamp, long/short action label, quantity, price, notional, TP/SL, decision source, risk decision, and rationale.
- Position policy metadata is now surfaced into reporting so open positions can show when the current episode began.

# v0.11.28 - Tighten intraday exits and teach reviews to flag giveback risk

Why:
- Several recent `SOL` episodes showed the same pattern: open profit reached roughly `+0.8% ~ +2.0%`, but the take-profit sat too far away and the first profit-lock stage sat too close to round-trip fees.
- That meant `TradePulse` could be directionally right, yet still give back too much of the move before realizing profit.
- The daily review layer also was not explicitly calling out this "too much giveback after correct directional read" pattern on its own.

What Changed:
- Tightened default perp exit settings for intraday trading:
  - `PERP_TAKE_PROFIT_PCT` from `2.4` to `1.8`
  - `PERP_PROFIT_LOCK_TRIGGER_PCT` from `1.0` to `0.8`
  - `PERP_PROFIT_LOCK_BREAKEVEN_OFFSET_PCT` from `0.10` to `0.25`
  - `PERP_PROFIT_LOCK_TRIGGER_2_PCT` from `2.0` to `1.5`
  - `PERP_PROFIT_LOCK_STOP_2_PCT` from `0.80` to `0.60`
- Existing perp positions now retarget stale take-profit orders toward the tighter current intraday target instead of preserving the older, wider TP forever.
- Daily review fallback analysis now explicitly flags cases where realized gains do not clear fees or where unrealized gains dominate realized results, and suggests tightening TP / first-stage profit-lock as a concrete next action.
- Strategy reflection now records the same giveback pattern in its risk-adjustment output so future learning controls can respond to it instead of only reacting to raw PnL.

# v0.11.29 - Make perp TP/SL regime-aware instead of one-size-fits-all

Why:
- Fixed-percent protection was still too blunt for `TradePulse`: quiet range days wanted closer targets, while clearer trend days could tolerate more room.
- We also needed the report itself to explain *which* protection regime was active, so `TP/SL` changes would not look arbitrary.

What Changed:
- Perp protection targets now derive a simple intraday regime from recent ATR, 20-candle range, and net-move efficiency.
- `quiet_range` profiles tighten both initial TP/SL and profit-lock triggers; `directional_trend` profiles allow slightly more room while still tightening giveback control; `normal` stays near the configured baseline.
- Daily reports and latest-decision summaries now show the active protection logic profile (`regime`, ATR, range, efficiency) alongside TP/SL.

# v0.11.30 - Make postmortems market-path aware and align episode times to local time

Why:
- Review and postmortem sections still leaned too heavily on decision counts and could miss what the actual sampled market path was doing inside the noon-to-noon window.
- Carry-in episodes could also show UTC-style opened timestamps, which made manual replay against local charts unnecessarily confusing.

What Changed:
- Daily reports now include a `Market Path Review` section that summarizes the sampled start/high/low/end path for the focus symbol, plus the largest down-leg and rebound inside the report window.
- `Symbol Postmortem` now references those sampled market moves directly, so reviews can call out things like "there was a clear down-leg and TradePulse still failed to flip short" instead of only restating `buy/sell/hold` counts.
- Carry-in and accepted-trade episode times are now normalized to Asia/Taipei local time in `Trade Review`, reducing the mismatch between report timestamps and what you see on local charts.

# v0.11.31 - Let shadow promotion watch follow the strongest eligible candidate

Why:
- Shadow promotion tracking was still hard-wired to `donchian_adx_keltner_v1`, which meant the whole watch could degrade into a `partial` state whenever that candidate was missing or not the most relevant one.
- In recent `SOL/USDT` windows, the actual standout candidate was often `grid_range_reversion_v1`, so the shadow watch needed to track the best eligible candidate instead of staying pinned to an older hypothesis.

What Changed:
- Shadow benchmark watch now auto-selects its watch candidate from a trusted candidate list, preferring the current symbol leader when it is not the live baseline.
- This allows `grid_range_reversion_v1` to enter the same formal promotion-streak flow as `donchian_adx_keltner_v1` instead of living only in benchmark commentary.
- Daily reports now stay in `ready` mode more often and can accumulate a meaningful promotion streak for the candidate that is actually winning on the focused symbol.

# v0.11.32 - Make learning controls react to repeated carry-in losses

Why:
- Recent noon windows showed the same unhealthy pattern repeating: carry-in positions were getting closed by `stagnation exit`, the window would still finish negative, and `TradePulse` would largely just preserve `fallback_entry_mode=base_only` without changing how long it tolerated weak carry-in exposure.
- That created the exact feeling the user called out: the system could describe losses, but it was not obviously growing or tightening its own behavior after repeated failures.

What Changed:
- Strategy reflection now looks at multi-window carry-in loss counts, stagnation-exit streaks, and repeated benchmark leadership instead of only fallback usage and cooldown blocks.
- New learning controls can switch on a `carry_in_mode=de_risk` posture and tighten `hold_bars_scale`, `stagnation_bars_scale`, and `stagnation_pnl_scale` when carry-in losses repeat.
- Intraday policy exits now consume those controls, so repeated carry-in failures lead to earlier de-risking in live behavior rather than remaining a report-only observation.
- Daily review text now explicitly calls out repeated carry-in drag and asks whether the new de-risk controls are actually reducing that pattern.

# v0.11.34 - Make strategy tournaments candidate-aware and alpha-aware

Why:
- Strategy tournament output had become harder to trust once benchmark candidates started using different execution-cost assumptions. The report still showed only the default benchmark cost model, which could make a maker-assumption candidate look like it was competing under the same friction as taker-style strategies.
- `--include-alpha` existed on the CLI, but the script was not actually replaying alpha-arena dataset candidates; they were silently skipped unless you inspected the code.

What Changed:
- `scripts/run_strategy_tournament.py` now replays alpha-arena dataset candidates when `--include-alpha` is set, rather than silently skipping them.
- Tournament rows now include the effective round-trip fee, slippage, funding, total cost, and whether a candidate used a custom cost model.
- Tournament Markdown output now surfaces candidate-specific cost overrides and lists skipped candidates with explicit reasons, making research review more trustworthy.
- External benchmark snapshots now preserve candidate-level cost metadata as well, so downstream daily reports can explain when a shadow winner depends on a custom maker-style assumption.

# v0.11.35 - Add idle-time strategy research cycles with anti-overfit checks

Why:
- TradePulse needed a way to keep learning about strategy candidates even when live trading is in a defensive or idle posture, without relying on ad hoc manual tournament runs.
- Pure single-window optimization would make it too easy to overfit recent `SOL/USDT` chop, so research needed both multiple lookback windows and a small validation set before stronger promotion decisions.

What Changed:
- Added `trading_agents/strategy_research.py`, which now owns reusable cost-aware tournament execution plus a multi-window strategy research cycle that aggregates focus-symbol and validation-symbol evidence.
- Added `scripts/run_strategy_research_cycle.py` for manual invocation of the same research loop used by the runner.
- The runner now performs low-frequency strategy research automatically during flat periods, using configurable intervals, candle windows, and validation symbols.
- New settings expose this behavior explicitly: `STRATEGY_RESEARCH_*` now controls whether idle-time research runs, how often it runs, which windows it tests, and which validation symbols it uses.
# v0.11.51 - Stop daily-review storms and prefilter weak candidates

Why:
- `daily-reviewer` could keep getting retriggered for the same noon window when the same completed summary fingerprint repeatedly failed through the LLM path, creating noisy trace files and unstable review quality.
- Strategy candidates that were already negative after costs and weak on profit factor were still flowing into risk approval, only to be rejected later by the fee hurdle. That created noisy proposals without adding useful learning.

What Changed:
- Added fingerprint-aware daily review resolution so a fallback/error review is persisted once per summary fingerprint and reused instead of retriggering the reviewer again in the same window.
- `DailyReviewAgent` now returns review metadata (`review_status`, `review_error`, `used_fallback`) alongside the snapshot so report artifacts can distinguish true LLM success from stable fallback output.
- Added a pre-risk candidate filter that converts clearly weak opening ideas into `hold` before the risk layer when the selected strategy replay is already negative after costs and weak on profit factor.
- Added regression tests to lock both behaviors: no repeated fallback-error review attempts for the same fingerprint, and pre-risk filtering of clearly weak candidates.

# v0.11.52 - Tighten low-sample entries and add fee-pressure throttling

Why:
- `2026-05-20` showed a familiar failure mode: more proposals cleared risk, but the realized edge was still too thin to survive fees.
- Low-sample strategy replays were still being allowed into live decision-making too easily, and countertrend longs were slipping through on down days without strong enough confirmation.

What Changed:
- Pre-risk candidate filtering is now stricter for low-sample setups: when recent replay has fewer than five trades, the candidate must already show positive expectancy and profit factor above 1.10 or it is downgraded to `hold`.
- Countertrend long entries now require stronger confirmation when the active signal is not bullish and order-flow still leans negative.
- Strategy reflection now watches recent realized-after-fees results and raises cooldown pressure when the last two windows were both net negative after fees, instead of continuing to loosen entry cadence into fee drag.
- Added regression tests to lock the low-sample prefilter and the fee-pressure cooldown response.
