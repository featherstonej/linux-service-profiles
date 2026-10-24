"""Unit tests for configuration schema, YAML persistence, StateManager, and Polkit rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from srvp.config import (
    DockerComposeStack,
    DockerConfig,
    Esp32SerialConfig,
    ProfileConfig,
    ProfilesFile,
    SystemdConfig,
    load_profiles,
    normalize_unit_name,
    save_profiles,
)
from srvp.polkit import generate_polkit_rules
from srvp.state import ManagedItem, StateManager
from srvp.templates import build_starter_profiles


def test_normalize_unit_name() -> None:
    assert normalize_unit_name("postgresql") == "postgresql.service"
    assert normalize_unit_name("docker.socket") == "docker.socket"
    assert normalize_unit_name("  redis-server  ") == "redis-server.service"
    with pytest.raises(ValueError):
        normalize_unit_name("   ")


def test_profile_effective_units() -> None:
    prof = ProfileConfig(
        description="Test profile",
        systemd=SystemdConfig(system=["postgresql"], ensure_stopped=["nginx"]),
        docker=DockerConfig(
            enabled=False,
            compose_stacks=[DockerComposeStack(name="app", file="docker-compose.yml")],
        ),
        esp32_serial=Esp32SerialConfig(enabled=True, stop_conflicting_services=True),
    )
    # docker.service is automatically added because compose_stacks is non-empty
    assert "postgresql.service" in prof.effective_system_units()
    assert "docker.service" in prof.effective_system_units()
    # brltty and ModemManager are automatically added to ensure_stopped
    stopped = prof.effective_ensure_stopped_units()
    assert "nginx.service" in stopped
    assert "brltty.service" in stopped
    assert "ModemManager.service" in stopped


def test_invalid_profile_name_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid profile name"):
        ProfilesFile(profiles={"bad name with spaces": ProfileConfig()})


def test_save_and_load_profiles(tmp_path: Path) -> None:
    cfg_path = tmp_path / "profiles.yaml"
    starter = build_starter_profiles()
    save_profiles(starter, cfg_path)
    loaded = load_profiles(cfg_path)
    assert set(loaded.profiles.keys()) == set(starter.profiles.keys())
    assert loaded.profiles["web-db"].systemd.system == [
        "postgresql.service",
        "redis-server.service",
    ]


def test_state_manager_roundtrip(tmp_path: Path) -> None:
    mgr = StateManager(tmp_path / "state.json")
    initial = mgr.load()
    assert initial.active_profile is None
    assert initial.active_items == []

    items = [
        ManagedItem(provider="systemd_system", identifier="postgresql.service"),
        ManagedItem(provider="adb", identifier="adb-server"),
    ]
    recorded = mgr.record_activation("web-db", items)
    assert recorded.active_profile == "web-db"
    assert recorded.activated_at is not None

    reloaded = mgr.load()
    assert reloaded.active_profile == "web-db"
    assert len(reloaded.active_items) == 2
    assert reloaded.active_items[0].key == ("systemd_system", "postgresql.service")


def test_generate_polkit_rules() -> None:
    starter = build_starter_profiles(redis_unit="redis.service")
    assert "redis.service" in starter.profiles["web-db"].systemd.system

    # Default (group=None) supports both sudo (Debian/Ubuntu) and wheel (Fedora/RHEL/Arch/openSUSE)
    default_rules = generate_polkit_rules(starter)
    assert "org.freedesktop.systemd1.manage-units" in default_rules
    assert 'subject.isInGroup("sudo")' in default_rules
    assert 'subject.isInGroup("wheel")' in default_rules
    assert '"postgresql.service"' in default_rules
    assert '"docker.service"' in default_rules

    # Explicit group override
    wheel_rules = generate_polkit_rules(starter, group="wheel")
    assert 'subject.isInGroup("wheel")' in wheel_rules
    assert 'subject.isInGroup("sudo")' not in wheel_rules
