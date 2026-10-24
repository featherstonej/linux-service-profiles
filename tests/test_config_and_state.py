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
    profiles_revision,
    save_profiles,
    save_profiles_if_unchanged,
)
from srvp.polkit import generate_polkit_rules
from srvp.state import ManagedItem, StateManager
from srvp.templates import build_starter_profiles


def test_normalize_unit_name() -> None:
    assert normalize_unit_name("postgresql") == "postgresql.service"
    assert normalize_unit_name("docker.socket") == "docker.socket"
    assert normalize_unit_name("data.automount") == "data.automount"
    assert normalize_unit_name("swapfile.swap") == "swapfile.swap"
    assert normalize_unit_name("dev-sda.device") == "dev-sda.device"
    assert normalize_unit_name("user.slice") == "user.slice"
    assert normalize_unit_name("session-1.scope") == "session-1.scope"
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


def test_duplicate_compose_stack_names_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate Docker Compose stack name 'backend'"):
        DockerConfig(
            compose_stacks=[
                DockerComposeStack(name="backend", file="/tmp/a/docker-compose.yml"),
                DockerComposeStack(name="backend", file="/tmp/b/docker-compose.yml"),
            ]
        )


def test_save_and_load_profiles(tmp_path: Path) -> None:
    cfg_path = tmp_path / "profiles.yaml"
    starter = build_starter_profiles(redis_unit="redis-server.service")
    save_profiles(starter, cfg_path)
    loaded = load_profiles(cfg_path)
    assert set(loaded.profiles.keys()) == set(starter.profiles.keys())
    assert loaded.profiles["web-db"].systemd.system == [
        "postgresql.service",
        "redis-server.service",
    ]


def test_stale_profile_save_is_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "profiles.yaml"
    initial = build_starter_profiles(redis_unit="redis-server.service")
    save_profiles(initial, cfg_path)
    stale = load_profiles(cfg_path)
    stale_revision = profiles_revision(stale)

    latest = load_profiles(cfg_path)
    latest.profiles["external-profile"] = ProfileConfig(description="Created by another writer")
    save_profiles(latest, cfg_path)

    stale.profiles.pop("docker-dev")
    with pytest.raises(ValueError, match="changed since it was loaded"):
        save_profiles_if_unchanged(stale, cfg_path, stale_revision)

    assert "external-profile" in load_profiles(cfg_path).profiles
    assert "docker-dev" in load_profiles(cfg_path).profiles


def test_state_manager_roundtrip_and_corrupt_detection(tmp_path: Path) -> None:
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

    # Corrupt state.json must raise ValueError instead of silently returning empty state
    (tmp_path / "state.json").write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt or unreadable state file"):
        mgr.load()


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
    assert '"docker.socket"' in default_rules

    # Explicit group override
    wheel_rules = generate_polkit_rules(starter, group="wheel")
    assert 'subject.isInGroup("wheel")' in wheel_rules
    assert 'subject.isInGroup("sudo")' not in wheel_rules

    # Escaping of backslashes, quotes, and newlines in unit names and group
    starter.profiles["web-db"].systemd.system.append(r"dev-disk\x2dby\x2duuid.service")
    escaped_rules = generate_polkit_rules(starter, group='adm"\n//inject')
    assert r'"dev-disk\\x2dby\\x2duuid.service"' in escaped_rules
    assert r'subject.isInGroup("adm\"\n//inject")' in escaped_rules
    assert "//inject" not in escaped_rules.splitlines()
