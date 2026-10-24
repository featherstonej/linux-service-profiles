"""Configuration schema and YAML loader/saver for Linux Service Profiles (srvp)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

VALID_UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".device",
    ".mount",
    ".automount",
    ".swap",
    ".target",
    ".path",
    ".timer",
    ".slice",
    ".scope",
)


def resolve_compose_paths(
    file_path: str,
    project_dir: str | None = None,
) -> tuple[str, str | None]:
    """Expand '~' and resolve Compose file_path and project_dir to absolute paths."""
    expanded_proj: Path | None = (
        Path(project_dir.strip()).expanduser().resolve()
        if (project_dir and project_dir.strip())
        else None
    )
    expanded_file = Path(file_path.strip()).expanduser()
    if not expanded_file.is_absolute():
        base_dir = expanded_proj if expanded_proj is not None else Path.cwd()
        resolved_file = (base_dir / expanded_file).resolve()
    else:
        resolved_file = expanded_file.resolve()
    return (
        str(resolved_file),
        str(expanded_proj) if expanded_proj is not None else None,
    )


def normalize_unit_name(unit: str) -> str:
    """Append .service if a systemd unit name does not specify a known unit suffix."""
    cleaned = unit.strip()
    if not cleaned:
        raise ValueError("Systemd unit name cannot be empty.")
    if not cleaned.endswith(VALID_UNIT_SUFFIXES):
        return f"{cleaned}.service"
    return cleaned


class SystemdConfig(BaseModel):
    """Systemd service unit specifications for a profile."""

    system: list[str] = Field(
        default_factory=list,
        description="System-level systemd units to start when this profile is active.",
    )
    user: list[str] = Field(
        default_factory=list,
        description="User-level (systemctl --user) units to start when this profile is active.",
    )
    ensure_stopped: list[str] = Field(
        default_factory=list,
        description="System-level systemd units that must be explicitly stopped in this profile.",
    )

    @field_validator("system", "user", "ensure_stopped", mode="before")
    @classmethod
    def _normalize_units(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("Expected a list of systemd unit names.")
        seen: list[str] = []
        for item in v:
            norm = normalize_unit_name(str(item))
            if norm not in seen:
                seen.append(norm)
        return seen


class DockerComposeStack(BaseModel):
    """A Docker Compose stack managed by a profile."""

    name: str = Field(..., min_length=1, description="Friendly identifier for this Compose stack.")
    file: str = Field(..., min_length=1, description="Path to docker-compose.yml / compose.yaml.")
    project_dir: str | None = Field(
        default=None,
        description="Optional working directory for docker compose.",
    )
    services: list[str] = Field(
        default_factory=list,
        description="Optional subset of compose services to start (empty starts all).",
    )

    @field_validator("name", "file", mode="before")
    @classmethod
    def _validate_non_empty_str(cls, v: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Compose stack name and file path must be non-empty strings.")
        return v.strip()


class DockerConfig(BaseModel):
    """Docker daemon, standalone container, and Compose stack configuration."""

    enabled: bool = Field(
        default=False,
        description="Ensure docker.service is running when this profile is active.",
    )
    containers: list[str] = Field(
        default_factory=list,
        description="Standalone Docker container names to start/stop.",
    )
    compose_stacks: list[DockerComposeStack] = Field(
        default_factory=list,
        description="Docker Compose stacks to bring up (-d) and down.",
    )

    @field_validator("containers", mode="before")
    @classmethod
    def _deduplicate_containers(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("Expected a list of container names.")
        seen: list[str] = []
        for item in v:
            cleaned = str(item).strip()
            if not cleaned:
                raise ValueError("Container name cannot be empty.")
            if cleaned not in seen:
                seen.append(cleaned)
        return seen

    @field_validator("compose_stacks")
    @classmethod
    def _validate_unique_compose_stack_names(
        cls, v: list[DockerComposeStack]
    ) -> list[DockerComposeStack]:
        seen_names: set[str] = set()
        for stack in v:
            if stack.name in seen_names:
                raise ValueError(
                    f"Duplicate Docker Compose stack name '{stack.name}': stack names within a profile must be unique."
                )
            seen_names.add(stack.name)
        return v


class AndroidConfig(BaseModel):
    """Android development tooling configuration."""

    adb_server: bool = Field(
        default=False,
        description="Start Android Debug Bridge (adb start-server) when active, stop when inactive.",
    )


class Esp32SerialConfig(BaseModel):
    """ESP32 and embedded serial port checks and conflict protection."""

    enabled: bool = Field(
        default=False,
        description="Enable serial port diagnostics and conflict resolution for ESP32/Arduino.",
    )
    check_dialout_group: bool = Field(
        default=True,
        description="Verify current user belongs to the 'dialout' or 'uucp' group.",
    )
    stop_conflicting_services: bool = Field(
        default=True,
        description="Automatically stop brltty.service and ModemManager.service to free ttyUSB.",
    )
    ports_glob: list[str] = Field(
        default_factory=lambda: ["/dev/ttyUSB*", "/dev/ttyACM*"],
        description="Glob patterns for serial device nodes to scan.",
    )


class HooksConfig(BaseModel):
    """Custom shell hooks executed around profile transitions."""

    pre_start: list[str] = Field(
        default_factory=list,
        description="Shell commands run before starting profile services.",
    )
    post_start: list[str] = Field(
        default_factory=list,
        description="Shell commands run after starting profile services.",
    )
    pre_stop: list[str] = Field(
        default_factory=list,
        description="Shell commands run before stopping profile services.",
    )
    post_stop: list[str] = Field(
        default_factory=list,
        description="Shell commands run after stopping profile services.",
    )


class ProfileConfig(BaseModel):
    """Complete definition of a single service profile."""

    description: str = Field(default="", description="Human-readable summary of the profile.")
    systemd: SystemdConfig = Field(default_factory=SystemdConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    android: AndroidConfig = Field(default_factory=AndroidConfig)
    esp32_serial: Esp32SerialConfig = Field(default_factory=Esp32SerialConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)

    def effective_system_units(self) -> list[str]:
        """Return system units to start, automatically including docker.service if docker is needed."""
        units = list(self.systemd.system)
        needs_docker = (
            self.docker.enabled or bool(self.docker.containers) or bool(self.docker.compose_stacks)
        )
        if needs_docker and "docker.service" not in units:
            units.append("docker.service")
        return units

    def effective_ensure_stopped_units(self) -> list[str]:
        """Return system units that must be stopped, including ESP32 conflict daemons and docker.socket."""
        stopped = list(self.systemd.ensure_stopped)
        eff_system = self.effective_system_units()
        if self.esp32_serial.enabled and self.esp32_serial.stop_conflicting_services:
            for conflict in ("brltty.service", "ModemManager.service"):
                if conflict not in stopped and conflict not in eff_system:
                    stopped.append(conflict)
        if (
            "docker.service" in stopped
            and "docker.service" not in eff_system
            and "docker.socket" not in stopped
            and "docker.socket" not in eff_system
        ):
            stopped.append("docker.socket")
        return stopped


class ProfilesFile(BaseModel):
    """Root schema for profiles.yaml."""

    version: int = Field(default=1, ge=1)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def _validate_profile_names(cls, v: dict[str, ProfileConfig]) -> dict[str, ProfileConfig]:
        for name in v:
            cleaned = name.strip()
            if not cleaned or " " in cleaned:
                raise ValueError(
                    f"Invalid profile name '{name}': profile names must be non-empty and contain no spaces."
                )
        return v


def resolve_config_path(custom_path: Path | str | None = None) -> Path:
    """Resolve the path to profiles.yaml using CLI flag, SRVP_CONFIG_PATH, or XDG default."""
    if custom_path is not None:
        return Path(custom_path).expanduser().resolve()
    env_path = os.environ.get("SRVP_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return (base / "linux-service-profiles" / "profiles.yaml").resolve()


def load_profiles(path: Path | str | None = None) -> ProfilesFile:
    """Load and validate profiles.yaml from disk."""
    resolved = resolve_config_path(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"Configuration file not found at '{resolved}'. Run 'srvp init' to generate starter profiles."
        )
    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at root of '{resolved}'.")
    return ProfilesFile.model_validate(data)


def save_profiles(config: ProfilesFile, path: Path | str | None = None) -> Path:
    """Serialize and save ProfilesFile to YAML on disk atomically."""
    resolved = resolve_config_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    dumped_dict = config.model_dump(mode="json")
    yaml_text = yaml.safe_dump(
        dumped_dict,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    header = (
        "# Linux Service Profiles (srvp) Configuration\n"
        "# Customize profiles below or edit via the interactive TUI ('srvp ui').\n\n"
    )
    content = header + yaml_text
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=resolved.parent,
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
        try:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    os.replace(tmp_path, resolved)
    return resolved
