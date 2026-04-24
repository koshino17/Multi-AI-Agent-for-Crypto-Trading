from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from trading_agents.config import Settings
from trading_agents.storage import build_storage_layout


RUNNER_LABEL = "com.koshino.trading-agents.runner"


def runner_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{RUNNER_LABEL}.plist"


def runner_launch_target() -> str:
    return f"gui/{os.getuid()}/{RUNNER_LABEL}"


def ensure_runner_launch_agent(settings: Settings, project_root: Path) -> tuple[Path, bool]:
    entrypoint_path = project_root / "run_tradepulse_runner.py"
    log_dir = Path.home() / "Library" / "Logs" / "TradePulse"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launchd-runner.log"
    plist_path = runner_launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    symbols = ",".join(settings.observation_pool) or settings.symbol
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{RUNNER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{entrypoint_path}</string>
    <string>--mode</string>
    <string>{settings.trading_mode}</string>
    <string>--symbol</string>
    <string>{symbols}</string>
    <string>--interval</string>
    <string>{settings.monitor_interval_seconds}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{project_root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{project_root}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
"""
    changed = True
    if plist_path.exists():
        try:
            changed = plist_path.read_text() != plist_content
        except OSError:
            changed = True
    if changed:
        plist_path.write_text(plist_content)
    return plist_path, changed


def is_runner_launch_agent_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "print", runner_launch_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def start_runner_service(settings: Settings, project_root: Path) -> dict[str, str]:
    storage = build_storage_layout(settings.data_root)
    plist_path, _ = ensure_runner_launch_agent(settings, project_root)
    _clear_stale_pid(storage.runner_supervisor_pid)
    _clear_stale_pid(storage.runner_pid)
    _clear_stale_lock(storage.notion_sync_lock, max_age_seconds=180)

    if is_runner_launch_agent_loaded():
        subprocess.run(["launchctl", "kickstart", "-k", runner_launch_target()], capture_output=True, text=True, check=False)
    else:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, text=True, check=False)
        subprocess.run(["launchctl", "enable", runner_launch_target()], capture_output=True, text=True, check=False)
        subprocess.run(["launchctl", "kickstart", "-k", runner_launch_target()], capture_output=True, text=True, check=False)

    deadline = time.time() + 8
    while time.time() < deadline:
        runner_pid = _read_pid(storage.runner_pid)
        if runner_pid and _pid_is_alive(runner_pid):
            return {
                "status": "started",
                "label": RUNNER_LABEL,
                "pid": str(runner_pid),
                "plist": str(plist_path),
                "command": f"/usr/bin/python3 {entrypoint_path}",
            }
        time.sleep(0.5)

    command = (
        f"cd {shlex.quote(str(project_root))} && "
        f"export PYTHONPATH={shlex.quote(str(project_root))} && "
        f"nohup /usr/bin/python3 {shlex.quote(str(entrypoint_path))} "
        f"--mode {shlex.quote(str(settings.trading_mode))} "
        f"--symbol {shlex.quote(','.join(settings.observation_pool) or settings.symbol)} "
        f"--interval {shlex.quote(str(settings.monitor_interval_seconds))} "
        f">> {shlex.quote(str(storage.runner_log))} 2>&1 &"
    )
    subprocess.run(["/bin/zsh", "-lc", command], capture_output=True, text=True, check=False)

    deadline = time.time() + 8
    while time.time() < deadline:
        runner_pid = _read_pid(storage.runner_pid)
        if runner_pid and _pid_is_alive(runner_pid):
            return {
                "status": "started",
                "label": "detached-runner",
                "pid": str(runner_pid),
                "plist": str(plist_path),
                "command": f"/usr/bin/python3 {entrypoint_path}",
            }
        time.sleep(0.5)
    raise RuntimeError("runner service failed to stay alive via launchd or detached fallback")


def stop_runner_service(settings: Settings) -> dict[str, str]:
    storage = build_storage_layout(settings.data_root)
    if is_runner_launch_agent_loaded():
        subprocess.run(["launchctl", "bootout", runner_launch_target()], capture_output=True, text=True, check=False)
        deadline = time.time() + 10
        while time.time() < deadline and is_runner_launch_agent_loaded():
            time.sleep(0.2)
    runner_pid = _read_pid(storage.runner_pid)
    if runner_pid and _pid_is_alive(runner_pid):
        try:
            os.kill(runner_pid, 15)
        except OSError:
            pass
    _clear_stale_pid(storage.runner_supervisor_pid)
    _clear_stale_pid(storage.runner_pid)
    return {"status": "stopped", "label": RUNNER_LABEL}


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _clear_stale_pid(path: Path) -> None:
    pid = _read_pid(path)
    if pid is None or _pid_is_alive(pid):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _clear_stale_lock(path: Path, max_age_seconds: int) -> None:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age <= max_age_seconds:
        return
    try:
        path.unlink()
    except OSError:
        pass
