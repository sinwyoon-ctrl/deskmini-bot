from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.redact import redact_text


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    returncode: int
    elapsed_ms: int


def run_fixed(argv: tuple[str, ...], timeout: float = 2.5, cwd: Path | None = None) -> CommandResult:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, _ = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            try:
                stdout, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, _ = process.communicate(timeout=3)
            returncode = 124
        output = redact_text((stdout or "").strip(), 8_000)
        return CommandResult(
            ok=returncode == 0,
            stdout=output,
            returncode=returncode,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        return CommandResult(
            ok=False,
            stdout=type(exc).__name__,
            returncode=127,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            first = raw.strip().split()[0]
            values[key] = int(first) * 1024
    except (OSError, ValueError, IndexError):
        return {}
    return values


def _uptime_seconds() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _docker_containers() -> dict[str, Any]:
    result = run_fixed(
        (
            "docker",
            "ps",
            "--all",
            "--format",
            "{{json .}}",
        ),
        timeout=3.0,
    )
    containers = []
    if result.ok:
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            containers.append(
                {
                    "name": item.get("Names"),
                    "state": item.get("State"),
                    "status": item.get("Status"),
                    "image": item.get("Image"),
                }
            )
    return {
        "ok": result.ok,
        "containers": containers,
        "error": None if result.ok else result.stdout,
        "elapsed_ms": result.elapsed_ms,
    }


def collect_host_status() -> dict[str, Any]:
    observed_at = datetime.now(UTC).isoformat(timespec="seconds")
    memory = _meminfo()
    usage = shutil.disk_usage("/")
    try:
        load = os.getloadavg()
    except OSError:
        load = ()
    tmux = run_fixed(("tmux", "has-session", "-t", "rc"), timeout=1.0)
    return {
        "observed_at": observed_at,
        "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "uptime_seconds": _uptime_seconds(),
        "load_average": list(load),
        "cpu_count": os.cpu_count(),
        "memory": {
            "total": memory.get("MemTotal"),
            "available": memory.get("MemAvailable"),
        },
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "remote_control": {
            "tmux_session": "active" if tmux.ok else ("inactive" if tmux.returncode == 1 else "unknown"),
            "evidence": tmux.stdout if not tmux.ok and tmux.returncode != 1 else None,
        },
        "docker": _docker_containers(),
    }


def bytes_human(value: int | None) -> str:
    if value is None:
        return "미확인"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TiB"
