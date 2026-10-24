"""Directory auto-switching (.srvp), port conflict diagnostics (doctor), and battery power triggers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor

# Well-known default TCP ports for common systemd services
KNOWN_SERVICE_PORTS: dict[str, list[int]] = {
    "postgresql.service": [5432],
    "redis-server.service": [6379],
    "redis.service": [6379],
    "nginx.service": [80, 443],
    "apache2.service": [80, 443],
    "httpd.service": [80, 443],
    "mosquitto.service": [1883],
    "mysql.service": [3306],
    "mariadb.service": [3306],
    "mongodb.service": [27017],
    "mongod.service": [27017],
}

# Process names considered owned by each unit so an already-running target service is not flagged as a conflict
EXPECTED_PROCESS_PREFIXES: dict[str, tuple[str, ...]] = {
    "postgresql.service": ("postgres", "postmaster"),
    "redis-server.service": ("redis",),
    "redis.service": ("redis",),
    "nginx.service": ("nginx",),
    "apache2.service": ("apache", "httpd"),
    "httpd.service": ("httpd", "apache"),
    "mosquitto.service": ("mosquitto",),
    "mysql.service": ("mysqld", "mariadbd"),
    "mariadb.service": ("mariadbd", "mysqld"),
    "mongodb.service": ("mongod",),
    "mongod.service": ("mongod",),
}


@dataclass(frozen=True)
class PortConflict:
    """Represents a TCP port required by a profile that is currently held by another process."""

    port: int
    expected_for: str
    bound_by_process: str
    bound_by_pid: int | None


def get_expected_ports_for_profile(profile: ProfileConfig) -> dict[int, str]:
    """Return a mapping of TCP port -> service/profile label expected by `profile`."""
    expected: dict[int, str] = {}
    for unit in profile.effective_system_units():
        for port in KNOWN_SERVICE_PORTS.get(unit, []):
            expected[port] = unit
    for port in profile.ports:
        expected.setdefault(port, "profile.ports")
    return expected


def detect_port_conflicts(
    profile: ProfileConfig,
    executor: CommandExecutor,
) -> list[PortConflict]:
    """Check whether any TCP ports required by `profile` are bound by conflicting processes."""
    expected_map = get_expected_ports_for_profile(profile)
    if not expected_map:
        return []

    listening = {lp.port: lp for lp in executor.get_listening_ports()}
    conflicts: list[PortConflict] = []

    for port, owner_unit in sorted(expected_map.items()):
        if port not in listening:
            continue
        bound = listening[port]
        allowed_prefixes = EXPECTED_PROCESS_PREFIXES.get(owner_unit, ())
        proc_lower = bound.process_name.lower()
        if allowed_prefixes and any(proc_lower.startswith(pref) for pref in allowed_prefixes):
            continue
        conflicts.append(
            PortConflict(
                port=port,
                expected_for=owner_unit,
                bound_by_process=bound.process_name,
                bound_by_pid=bound.pid,
            )
        )
    return conflicts


def find_srvp_file(start_dir: Path | str | None = None) -> tuple[Path, str] | None:
    """Walk up from `start_dir` (default CWD) looking for a `.srvp` file containing a profile name."""
    cur = Path(start_dir).expanduser().resolve() if start_dir else Path.cwd().resolve()
    if cur.is_file():
        cur = cur.parent

    for directory in (cur, *cur.parents):
        candidate = directory / ".srvp"
        try:
            if candidate.is_file():
                for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if line and not line.startswith("#"):
                        return candidate, line
        except OSError:
            continue
    return None


def generate_shell_hook(shell: str = "bash") -> str:
    """Generate a shell prompt hook snippet for automatic directory-based `.srvp` switching."""
    sh = shell.lower().strip()
    if sh == "zsh":
        return """# srvp directory auto-switch hook for zsh (add to ~/.zshrc)
_srvp_chpwd_hook() {
    if [ -f .srvp ]; then
        srvp auto --quiet
    fi
}
autoload -U add-zsh-hook
add-zsh-hook chpwd _srvp_chpwd_hook
"""
    if sh == "fish":
        return """# srvp directory auto-switch hook for fish (add to ~/.config/fish/config.fish)
function _srvp_pwd_hook --on-variable PWD
    if test -f .srvp
        srvp auto --quiet
    end
end
"""
    return """# srvp directory auto-switch hook for bash (add to ~/.bashrc)
_srvp_prompt_hook() {
    if [ "$PWD" != "$_SRVP_LAST_PWD" ]; then
        _SRVP_LAST_PWD="$PWD"
        if [ -f .srvp ]; then
            srvp auto --quiet
        fi
    fi
}
PROMPT_COMMAND="_srvp_prompt_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
"""


def generate_power_udev_rule(threshold: int = 20, target_profile: str = "idle") -> str:
    """Generate a udev rule snippet that runs `srvp power-check` on AC unplug / battery events."""
    return f"""# Generated by Linux Service Profiles (srvp)
# Save to: /etc/udev/rules.d/99-srvp-power.rules
# Reload with: sudo udevadm control --reload-rules
# Automatically switches to '{target_profile}' when on battery at or below {threshold}%.

SUBSYSTEM=="power_supply", ATTR{{online}}=="0", RUN+="/usr/bin/env srvp power-check --threshold {threshold} --target-profile {target_profile}"
"""
