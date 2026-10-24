"""Unit tests for the exclusive transition and diff engine."""

from __future__ import annotations

from dataclasses import replace

from srvp.config import DockerComposeStack, ProfilesFile
from srvp.engine import ProfileEngine
from srvp.executor import MockCommandExecutor
from srvp.state import StateManager


def test_exclusive_switch_stops_previous_profile_services(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)

    # 1. Switch to web-db
    res1 = engine.switch_profile("web-db")
    assert res1.success is True
    assert state_manager.load().active_profile == "web-db"
    assert "postgresql.service" in mock_executor.active_system_units
    assert "redis-server.service" in mock_executor.active_system_units

    # 2. Switch to android-dev (exclusive switch should stop postgresql & redis-server, start adb)
    res2 = engine.switch_profile("android-dev")
    assert res2.success is True
    assert state_manager.load().active_profile == "android-dev"
    assert "postgresql.service" not in mock_executor.active_system_units
    assert "redis-server.service" not in mock_executor.active_system_units
    assert mock_executor.adb_running is True

    # 3. Switch to esp32-embedded (stops adb-server, stops ModemManager & brltty)
    res3 = engine.switch_profile("esp32-embedded")
    assert res3.success is True
    assert mock_executor.adb_running is False
    assert "ModemManager.service" not in mock_executor.active_system_units

    # 4. Stop all (return to idle)
    res4 = engine.stop_all()
    assert res4.success is True
    assert state_manager.load().active_profile is None


def test_dry_run_does_not_mutate_state_or_services(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    dry_exec = replace(mock_executor, dry_run=True)
    engine = ProfileEngine(starter_profiles, state_manager, dry_exec)

    res = engine.switch_profile("web-db")
    assert res.dry_run is True
    assert len(res.records) > 0
    assert all(r.dry_run for r in res.records)
    assert state_manager.load().active_profile is None
    assert "postgresql.service" not in dry_exec.active_system_units


def test_validate_config_detects_missing_compose_and_overlap(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    starter_profiles.profiles["docker-dev"].docker.compose_stacks.append(
        DockerComposeStack(name="missing-stack", file="/nonexistent/docker-compose.yml")
    )
    starter_profiles.profiles["web-db"].systemd.ensure_stopped.append("postgresql.service")

    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    warnings = engine.validate_config()
    assert len(warnings) == 2
    assert any("postgresql.service" in w for w in warnings)
    assert any("missing-stack" in w for w in warnings)
