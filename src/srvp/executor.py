"""Pluggable command execution backend (Real, Dry-Run, and Mock for autonomous testing)."""

from __future__ import annotations

import glob as py_glob
import grp
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import psutil


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a shell or subprocess command."""

    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False

    @property
    def command_str(self) -> str:
        return shlex.join(self.command)


@dataclass(frozen=True)
class ProcessMetrics:
    """Resource usage snapshot for a running service PID."""

    pid: int | None
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


class CommandExecutor(Protocol):
    """Protocol for executing system commands and querying host status."""

    @property
    def is_dry_run(self) -> bool: ...

    def run(
        self,
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult: ...

    def get_process_metrics(self, pid: int) -> ProcessMetrics: ...

    def glob_paths(self, pattern: str) -> list[str]: ...

    def get_user_groups(self) -> list[str]: ...

    def file_exists(self, path: str) -> bool: ...


class RealCommandExecutor:
    """Executes real commands on the Linux host, respecting --dry-run for mutations."""

    def __init__(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self.history: list[CommandResult] = []

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    def run(
        self,
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        full_cmd = ["sudo", *cmd] if sudo and os.geteuid() != 0 else list(cmd)
        if self._dry_run and mutating:
            res = CommandResult(
                command=full_cmd,
                returncode=0,
                stdout=f"[DRY-RUN] Would execute: {shlex.join(full_cmd)}",
                stderr="",
                dry_run=True,
            )
            self.history.append(res)
            return res

        try:
            if shell:
                completed = subprocess.run(
                    full_cmd[0] if len(full_cmd) == 1 else shlex.join(full_cmd),
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            else:
                completed = subprocess.run(
                    full_cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            res = CommandResult(
                command=full_cmd,
                returncode=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
                dry_run=False,
            )
        except Exception as exc:
            res = CommandResult(
                command=full_cmd,
                returncode=1,
                stdout="",
                stderr=str(exc),
                dry_run=False,
            )
        self.history.append(res)
        return res

    def get_process_metrics(self, pid: int) -> ProcessMetrics:
        if pid <= 0:
            return ProcessMetrics(pid=None)
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=None)
            mem_bytes = proc.memory_info().rss
            return ProcessMetrics(
                pid=pid,
                cpu_percent=round(cpu, 1),
                memory_mb=round(mem_bytes / (1024 * 1024), 1),
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return ProcessMetrics(pid=pid, cpu_percent=0.0, memory_mb=0.0)

    def glob_paths(self, pattern: str) -> list[str]:
        return sorted(py_glob.glob(pattern))

    def get_user_groups(self) -> list[str]:
        groups: list[str] = []
        try:
            for gid in os.getgroups():
                try:
                    groups.append(grp.getgrgid(gid).gr_name)
                except KeyError:
                    continue
        except Exception:
            pass
        return groups

    def file_exists(self, path: str) -> bool:
        return Path(path).expanduser().exists()


@dataclass
class MockCommandExecutor:
    """Deterministic in-memory system simulator for tests and --mock mode."""

    dry_run: bool = False
    state_file: Path | None = None
    active_system_units: set[str] = field(default_factory=lambda: {"ModemManager.service"})
    active_user_units: set[str] = field(default_factory=set)
    active_containers: set[str] = field(default_factory=set)
    active_compose_files: set[str] = field(default_factory=set)
    adb_running: bool = False
    serial_ports: list[str] = field(default_factory=lambda: ["/dev/ttyUSB0"])
    user_groups: list[str] = field(default_factory=lambda: ["james", "sudo", "dialout"])
    existing_files: set[str] = field(default_factory=set)
    history: list[CommandResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.active_system_units = set(data.get("active_system_units", []))
                self.active_user_units = set(data.get("active_user_units", []))
                self.active_containers = set(data.get("active_containers", []))
                self.active_compose_files = set(data.get("active_compose_files", []))
                self.adb_running = bool(data.get("adb_running", False))
            except Exception:
                pass

    def _persist_mock_state(self) -> None:
        if not self.state_file or self.dry_run:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_system_units": sorted(self.active_system_units),
            "active_user_units": sorted(self.active_user_units),
            "active_containers": sorted(self.active_containers),
            "active_compose_files": sorted(self.active_compose_files),
            "adb_running": self.adb_running,
        }
        self.state_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run

    def run(
        self,
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        full_cmd = ["sudo", *cmd] if sudo else list(cmd)
        if self.dry_run and mutating:
            res = CommandResult(
                command=full_cmd,
                returncode=0,
                stdout=f"[DRY-RUN] Would execute: {shlex.join(full_cmd)}",
                stderr="",
                dry_run=True,
            )
            self.history.append(res)
            return res

        stdout = ""
        returncode = 0

        # Simulate systemctl (system and --user)
        if cmd and cmd[0] == "systemctl":
            is_user = "--user" in cmd
            target_set = self.active_user_units if is_user else self.active_system_units
            args = [c for c in cmd[1:] if c != "--user"]
            if args:
                sub = args[0]
                units = [u for u in args[1:] if not u.startswith("-")]
                if sub == "start":
                    for u in units:
                        target_set.add(u)
                    stdout = f"Started {', '.join(units)}"
                elif sub == "stop":
                    for u in units:
                        target_set.discard(u)
                    stdout = f"Stopped {', '.join(units)}"
                elif sub == "is-active" and units:
                    unit = units[0]
                    if unit in target_set:
                        stdout = "active"
                        returncode = 0
                    else:
                        stdout = "inactive"
                        returncode = 3
                elif sub == "show" and units:
                    unit = units[0]
                    pid = (abs(hash(unit)) % 8000 + 1000) if unit in target_set else 0
                    state_str = "active" if unit in target_set else "inactive"
                    stdout = f"ActiveState={state_str}\nMainPID={pid}"

        # Simulate docker and docker compose
        elif cmd and cmd[0] == "docker":
            if len(cmd) >= 2 and cmd[1] == "start":
                for c in cmd[2:]:
                    self.active_containers.add(c)
                stdout = "\n".join(cmd[2:])
            elif len(cmd) >= 2 and cmd[1] == "stop":
                for c in cmd[2:]:
                    self.active_containers.discard(c)
                stdout = "\n".join(cmd[2:])
            elif len(cmd) >= 2 and cmd[1] == "inspect":
                target = cmd[-1]
                if target in self.active_containers:
                    pid = abs(hash(target)) % 8000 + 2000
                    stdout = f"true|{pid}"
                else:
                    stdout = "false|0"
            elif len(cmd) >= 2 and cmd[1] == "compose":
                compose_file = ""
                if "-f" in cmd:
                    f_idx = cmd.index("-f")
                    if f_idx + 1 < len(cmd):
                        compose_file = cmd[f_idx + 1]
                if "up" in cmd:
                    self.active_compose_files.add(compose_file)
                    stdout = f"Compose stack {compose_file} started"
                elif "down" in cmd:
                    self.active_compose_files.discard(compose_file)
                    stdout = f"Compose stack {compose_file} stopped"
                elif "ps" in cmd:
                    stdout = "running" if compose_file in self.active_compose_files else ""

        # Simulate adb
        elif cmd and cmd[0] == "adb":
            if "start-server" in cmd:
                self.adb_running = True
                stdout = "* daemon started successfully"
            elif "kill-server" in cmd:
                self.adb_running = False
                stdout = "* server stopped"
            elif "devices" in cmd:
                stdout = (
                    "List of devices attached\nemulator-5554\tdevice" if self.adb_running else ""
                )

        # Simulate pgrep for adb
        elif cmd and cmd[0] == "pgrep":
            if self.adb_running:
                stdout = "4242"
                returncode = 0
            else:
                stdout = ""
                returncode = 1

        # Simulate custom shell hooks
        elif shell:
            stdout = f"Executed hook: {cmd[0]}"

        self._persist_mock_state()
        res = CommandResult(
            command=full_cmd,
            returncode=returncode,
            stdout=stdout,
            stderr="",
            dry_run=False,
        )
        self.history.append(res)
        return res

    def get_process_metrics(self, pid: int) -> ProcessMetrics:
        if not pid or pid <= 0:
            return ProcessMetrics(pid=None, cpu_percent=0.0, memory_mb=0.0)
        cpu = round(((pid % 17) + 1) * 0.4, 1)
        mem = round(((pid % 40) + 12) * 3.5, 1)
        return ProcessMetrics(pid=pid, cpu_percent=cpu, memory_mb=mem)

    def glob_paths(self, pattern: str) -> list[str]:
        if "ttyUSB" in pattern:
            return [p for p in self.serial_ports if "ttyUSB" in p]
        if "ttyACM" in pattern:
            return [p for p in self.serial_ports if "ttyACM" in p]
        return list(self.serial_ports)

    def get_user_groups(self) -> list[str]:
        return list(self.user_groups)

    def file_exists(self, path: str) -> bool:
        if path in self.existing_files:
            return True
        return Path(path).expanduser().exists()
