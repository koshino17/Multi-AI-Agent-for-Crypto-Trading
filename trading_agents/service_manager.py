from __future__ import annotations

import os
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
    script_path = project_root / "scripts" / "launch_trading_runner.sh"
    log_path = Path(settings.data_root).expanduser() / "service" / "launchd-runner.log"
    plist_path = runner_launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{RUNNER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>{script_path}</string>
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
    script_path = project_root / "scripts" / "run_trading_supervisor.sh"
    if not script_path.exists():
        raise RuntimeError(f"Missing supervisor script: {script_path}")

    if is_runner_launch_agent_loaded():
        subprocess.run(["launchctl", "bootout", runner_launch_target()], capture_output=True, text=True, check=False)

    supervisor_pid = _read_pid(storage.runner_supervisor_pid)
    if supervisor_pid and _pid_is_alive(supervisor_pid):
        return {
            "status": "started",
            "label": "runner-supervisor",
            "pid": str(supervisor_pid),
            "script": str(script_path),
        }

    _clear_stale_pid(storage.runner_supervisor_pid)
    _clear_stale_pid(storage.runner_pid)
    _clear_stale_lock(storage.notion_sync_lock, max_age_seconds=180)

    log_handle = storage.runner_supervisor_log.open("ab")
    process = subprocess.Popen(
        ["/bin/zsh", str(script_path)],
        cwd=project_root,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
        start_new_session=True,
    )
    log_handle.close()

    deadline = time.time() + 10
    while time.time() < deadline:
        supervisor_pid = _read_pid(storage.runner_supervisor_pid)
        if supervisor_pid and _pid_is_alive(supervisor_pid):
            return {
                "status": "started",
                "label": "runner-supervisor",
                "pid": str(supervisor_pid),
                "script": str(script_path),
            }
        if process.poll() is not None:
            break
        time.sleep(0.2)
    raise RuntimeError("runner supervisor failed to stay alive")


def stop_runner_service(settings: Settings) -> dict[str, str]:
    storage = build_storage_layout(settings.data_root)
    supervisor_pid = _read_pid(storage.runner_supervisor_pid)
    if supervisor_pid and _pid_is_alive(supervisor_pid):
        try:
            os.kill(supervisor_pid, 15)
        except OSError:
            pass
        deadline = time.time() + 10
        while time.time() < deadline and _pid_is_alive(supervisor_pid):
            time.sleep(0.2)
    _clear_stale_pid(storage.runner_supervisor_pid)
    _clear_stale_pid(storage.runner_pid)
    return {"status": "stopped", "label": "runner-supervisor"}


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
