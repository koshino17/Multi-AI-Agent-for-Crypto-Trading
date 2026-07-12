from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from trading_agents.config import Settings
from trading_agents.storage import build_storage_layout, mode_storage_root


RUNNER_LABEL = "com.koshino.trading-agents.runner"
RUNNER_PROCESS_MARKER = "run_tradepulse_runner.py"
MAX_RUNNER_LOG_BYTES = 64 * 1024 * 1024


def runner_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{RUNNER_LABEL}.plist"


def runner_launch_target() -> str:
    return f"gui/{os.getuid()}/{RUNNER_LABEL}"


def runner_runtime_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "TradePulse" / "runtime"


def runner_runtime_launcher_path(runtime_root: Path) -> Path:
    return runtime_root / "scripts" / "launch_trading_runner.sh"


def runner_state_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "TradePulse" / "state"


def runner_service_storage(mode: str = "bybit-demo-perp"):
    return build_storage_layout(str(mode_storage_root(runner_state_root(), mode)))


def runtime_python_path(runtime_root: Path) -> Path:
    return runtime_root / ".venv" / "bin" / "python3"


def preferred_python(project_root: Path, runtime_root: Path | None = None) -> Path:
    if runtime_root is not None:
        runtime_python = runtime_python_path(runtime_root)
        if runtime_python.exists():
            return runtime_python
    venv_python = project_root / ".venv" / "bin" / "python3"
    if venv_python.exists():
        return venv_python
    current_python = Path(sys.executable)
    if current_python.exists():
        return current_python
    return Path("/usr/bin/python3")


def _parse_env_lines(lines: list[str]) -> tuple[list[str], dict[str, str]]:
    ordered_keys: list[str] = []
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key not in values:
            ordered_keys.append(key)
        values[key] = value
    return ordered_keys, values


def sync_runner_runtime(project_root: Path) -> Path:
    runtime_root = runner_runtime_root()
    runtime_root.mkdir(parents=True, exist_ok=True)

    def _copy_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    package_source = project_root / "trading_agents"
    package_target = runtime_root / "trading_agents"
    if package_target.exists():
        shutil.rmtree(package_target)
    shutil.copytree(package_source, package_target)

    config_source = project_root / "config"
    config_target = runtime_root / "config"
    if config_target.exists():
        shutil.rmtree(config_target)
    shutil.copytree(config_source, config_target)

    env_source = project_root / ".env"
    env_target = runtime_root / ".env"
    env_lines: list[str] = []
    target_lines: list[str] = []
    if env_source.exists():
        env_lines = env_source.read_text().splitlines()
    if env_target.exists():
        target_lines = env_target.read_text().splitlines()
    target_order, target_values = _parse_env_lines(target_lines)
    source_order, source_values = _parse_env_lines(env_lines)
    merged_order = list(target_order)
    for key in source_order:
        if key not in merged_order:
            merged_order.append(key)
    merged_values = dict(target_values)
    merged_values.update(source_values)
    state_root = runner_state_root()
    if "DATA_ROOT" not in merged_order:
        merged_order.append("DATA_ROOT")
    merged_values["DATA_ROOT"] = str(state_root)
    rendered_lines = [f"{key}={merged_values[key]}" for key in merged_order if key in merged_values]
    env_target.write_text("\n".join(rendered_lines) + ("\n" if rendered_lines else ""))

    entrypoint_source = project_root / "run_tradepulse_runner.py"
    if entrypoint_source.exists():
        _copy_file(entrypoint_source, runtime_root / "run_tradepulse_runner.py")

    launcher_source = project_root / "scripts" / "launch_trading_runner.sh"
    if launcher_source.exists():
        launcher_target = runner_runtime_launcher_path(runtime_root)
        _copy_file(launcher_source, launcher_target)
        launcher_target.chmod(0o755)

    venv_source = project_root / ".venv"
    venv_target = runtime_root / ".venv"
    if venv_source.exists():
        if venv_target.exists():
            shutil.rmtree(venv_target)
        shutil.copytree(venv_source, venv_target, symlinks=True)
        for path in venv_target.rglob("distutils-precedence.pth"):
            try:
                path.unlink()
            except OSError:
                pass

    return runtime_root


def _runner_launch_agent_plist(runtime_root: Path, log_path: Path) -> str:
    launcher_path = runner_runtime_launcher_path(runtime_root)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{RUNNER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>{launcher_path}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{runtime_root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{runtime_root}</string>
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


def ensure_runner_launch_agent(settings: Settings, project_root: Path) -> tuple[Path, bool]:
    runtime_root = sync_runner_runtime(project_root)
    log_dir = Path.home() / "Library" / "Logs" / "TradePulse"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launchd-runner.log"
    _rotate_large_log(log_path)
    plist_path = runner_launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_content = _runner_launch_agent_plist(runtime_root, log_path)
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
    storage = runner_service_storage(settings.trading_mode)
    _rotate_large_log(storage.runner_log)
    runtime_root = sync_runner_runtime(project_root)
    entrypoint_path = runtime_root / "run_tradepulse_runner.py"
    python_path = preferred_python(project_root, runtime_root)
    plist_path, _plist_changed = ensure_runner_launch_agent(settings, project_root)
    _clear_stale_pid(storage.runner_supervisor_pid)
    _clear_stale_pid(storage.runner_pid)
    _clear_stale_lock(storage.notion_sync_lock, max_age_seconds=180)

    if is_runner_launch_agent_loaded():
        subprocess.run(["launchctl", "bootout", runner_launch_target()], capture_output=True, text=True, check=False)
        deadline = time.time() + 10
        while time.time() < deadline and is_runner_launch_agent_loaded():
            time.sleep(0.2)
    _terminate_runner_processes()

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
                "command": f"{python_path} {entrypoint_path}",
            }
        time.sleep(0.5)

    command = (
        f"cd {shlex.quote(str(runtime_root))} && "
        f"export PYTHONPATH={shlex.quote(str(runtime_root))} && "
        f"nohup {shlex.quote(str(python_path))} {shlex.quote(str(entrypoint_path))} "
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
                "command": f"{python_path} {entrypoint_path}",
            }
        time.sleep(0.5)
    raise RuntimeError("runner service failed to stay alive via launchd or detached fallback")


def stop_runner_service(settings: Settings) -> dict[str, str]:
    storage = runner_service_storage(settings.trading_mode)
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
    _terminate_runner_processes()
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


def _runner_process_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", RUNNER_PROCESS_MARKER],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _terminate_runner_processes() -> None:
    pids = _runner_process_pids()
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_pid_is_alive(pid) for pid in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        if _pid_is_alive(pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _rotate_large_log(path: Path, max_bytes: int = MAX_RUNNER_LOG_BYTES) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        rotated = path.with_name(f"{path.name}.1")
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except OSError:
        pass
