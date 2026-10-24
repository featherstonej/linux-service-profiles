"""Unit tests for the exclusive transition and diff engine."""

from __future__ import annotations

from dataclasses import replace

from srvp.config import (
    DockerComposeStack,
    DockerConfig,
    HooksConfig,
    ProfileConfig,
    ProfilesFile,
    SystemdConfig,
)
from srvp.engine import ProfileEngine
from srvp.executor import CommandResult, MockCommandExecutor
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


def test_compose_metadata_change_stops_old_stack_before_starting_new(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    starter_profiles.profiles["stack-a"] = ProfileConfig(
        docker=DockerConfig(
            enabled=True,
            compose_stacks=[DockerComposeStack(name="app", file="/tmp/a/compose.yml")],
        )
    )
    starter_profiles.profiles["stack-b"] = ProfileConfig(
        docker=DockerConfig(
            enabled=True,
            compose_stacks=[DockerComposeStack(name="app", file="/tmp/b/compose.yml")],
        )
    )
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)

    engine.switch_profile("stack-a")
    assert "/tmp/a/compose.yml" in mock_executor.active_compose_files

    engine.switch_profile("stack-b")
    assert "/tmp/a/compose.yml" not in mock_executor.active_compose_files
    assert "/tmp/b/compose.yml" in mock_executor.active_compose_files


def test_failed_stop_aborts_dependent_shutdown_and_preserves_state(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    starter_profiles.profiles["docker-dev"].docker.containers = ["app-container"]
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)

    res1 = engine.switch_profile("docker-dev")
    assert res1.success is True
    assert "docker.service" in mock_executor.active_system_units
    assert "app-container" in mock_executor.active_containers

    orig_run = mock_executor.run

    def fail_container_stop(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if cmd[:2] == ["docker", "stop"]:
            res = CommandResult(
                command=cmd,
                returncode=1,
                stdout="",
                stderr="Failed to stop container",
                dry_run=False,
            )
            mock_executor.history.append(res)
            return res
        return orig_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = fail_container_stop  # type: ignore[method-assign]

    # Switching to web-db should fail during docker_container stop, aborting docker.service stop
    # and aborting web-db startup.
    res2 = engine.switch_profile("web-db")
    assert res2.success is False
    assert "docker.service" in mock_executor.active_system_units
    assert "postgresql.service" not in mock_executor.active_system_units
    saved = state_manager.load()
    assert saved.active_profile == "docker-dev"
    assert ("docker_container", "app-container") in {it.key for it in saved.active_items}


def test_partial_batch_outcomes_reconciled_in_state(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    engine.switch_profile("web-db")

    orig_run = mock_executor.run

    def partial_stop(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if cmd[:2] == ["systemctl", "stop"]:
            # Simulate postgresql stopping while redis-server fails to stop
            mock_executor.active_system_units.discard("postgresql.service")
            res = CommandResult(
                command=["sudo", *cmd] if sudo else cmd,
                returncode=1,
                stdout="",
                stderr="Failed to stop redis-server.service",
                dry_run=False,
            )
            mock_executor.history.append(res)
            return res
        return orig_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = partial_stop  # type: ignore[method-assign]
    stop_res = engine.stop_all()
    assert stop_res.success is False
    saved = state_manager.load()
    assert saved.active_profile == "web-db"
    remaining_ids = [it.identifier for it in saved.active_items]
    assert "postgresql.service" not in remaining_ids
    assert "redis-server.service" in remaining_ids


def test_transition_serialized_with_interprocess_flock(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    import fcntl

    import pytest

    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    orig_run = mock_executor.run
    observed_locked_during_exec = False

    def check_lock_during_run(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        nonlocal observed_locked_during_exec
        with state_manager.lock_path.open("a+", encoding="utf-8") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed_locked_during_exec = True
        return orig_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = check_lock_during_run  # type: ignore[method-assign]
    engine.switch_profile("web-db")
    assert observed_locked_during_exec is True


def test_stop_uses_activation_snapshot_and_compose_resolves_relative_paths(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    engine.switch_profile("web-db")

    # Simulate nginx.service running externally and user editing web-db config without re-applying
    mock_executor.active_system_units.add("nginx.service")
    starter_profiles.profiles["web-db"].systemd.system.append("nginx.service")

    engine.stop_all()
    # nginx.service was not part of the recorded activation snapshot, so it must stay running
    assert "nginx.service" in mock_executor.active_system_units

    # Verify validate_config resolves relative compose file against project_dir (with ~ expansion)
    mock_executor.existing_files.add("/tmp/myproj/docker-compose.yml")
    starter_profiles.profiles["docker-dev"].docker.compose_stacks = [
        DockerComposeStack(
            name="rel-stack",
            file="docker-compose.yml",
            project_dir="/tmp/myproj",
        )
    ]
    assert engine.validate_config() == []


def test_failed_start_preserves_previously_tracked_and_partially_started_items(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    from srvp.config import SystemdConfig

    # Profile A starts postgresql.service
    starter_profiles.profiles["prof-a"] = ProfileConfig(
        systemd=SystemdConfig(system=["postgresql.service"])
    )
    # Profile B keeps postgresql.service and adds compose stack 'partial-stack'
    starter_profiles.profiles["prof-b"] = ProfileConfig(
        systemd=SystemdConfig(system=["postgresql.service"]),
        docker=DockerConfig(
            enabled=True,
            compose_stacks=[
                DockerComposeStack(
                    name="partial-stack",
                    file="/tmp/partial/compose.yml",
                    services=["api", "worker"],
                )
            ],
        ),
    )
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    assert engine.switch_profile("prof-a").success is True

    orig_run = mock_executor.run

    def fail_compose_up_after_partial_start(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if cmd[:2] == ["docker", "compose"] and "up" in cmd:
            # Simulate partial container creation before compose up exits non-zero
            mock_executor.active_compose_files.add("/tmp/partial/compose.yml")
            res = CommandResult(
                command=cmd,
                returncode=1,
                stdout="",
                stderr="worker container failed to start",
                dry_run=False,
            )
            mock_executor.history.append(res)
            return res
        return orig_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = fail_compose_up_after_partial_start  # type: ignore[method-assign]
    res_b = engine.switch_profile("prof-b")
    assert res_b.success is False
    saved = state_manager.load()
    saved_keys = {it.key for it in saved.active_items}
    # Both previously active postgresql.service and partially started compose stack remain tracked
    assert ("systemd_system", "postgresql.service") in saved_keys
    assert ("docker_compose", "partial-stack") in saved_keys


def test_failed_start_tracks_systemd_unit_still_activating(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    from srvp.config import SystemdConfig

    starter_profiles.profiles["slow-start"] = ProfileConfig(
        systemd=SystemdConfig(system=["slow.service"])
    )
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)
    orig_run = mock_executor.run

    def fail_start_while_activating(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if cmd[:2] == ["systemctl", "start"]:
            res = CommandResult(
                command=["sudo", *cmd],
                returncode=1,
                stdout="",
                stderr="Start operation timed out",
                dry_run=False,
            )
            mock_executor.history.append(res)
            return res
        if cmd[:2] == ["systemctl", "show"] and "--property=ActiveState" in cmd:
            res = CommandResult(
                command=cmd,
                returncode=0,
                stdout="ActiveState=activating",
                stderr="",
                dry_run=False,
            )
            mock_executor.history.append(res)
            return res
        return orig_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = fail_start_while_activating  # type: ignore[method-assign]

    result = engine.switch_profile("slow-start")

    assert result.success is False
    assert [it.identifier for it in state_manager.load().active_items] == ["slow.service"]

    stop_result = engine.stop_all()

    assert stop_result.success is True
    assert any(
        record.action == "stop" and record.identifier == "slow.service"
        for record in stop_result.records
    )
    assert state_manager.load().active_items == []


def test_failed_post_start_hook_retains_teardown_hooks(
    starter_profiles: ProfilesFile,
    state_manager: StateManager,
    mock_executor: MockCommandExecutor,
) -> None:
    starter_profiles.profiles["hooked"] = ProfileConfig(
        systemd=SystemdConfig(system=["service-for-hook.service"]),
        hooks=HooksConfig(
            pre_stop=["cleanup-before"],
            post_start=["fail-after-start"],
            post_stop=["cleanup-after"],
        ),
    )
    original_run = mock_executor.run
    hook_calls: list[str] = []

    def fail_post_start(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if shell:
            hook_calls.append(cmd[0])
            if cmd[0] == "fail-after-start":
                result = CommandResult(cmd, 1, stderr="hook failed")
                mock_executor.history.append(result)
                return result
        return original_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = fail_post_start  # type: ignore[method-assign]
    engine = ProfileEngine(starter_profiles, state_manager, mock_executor)

    assert engine.switch_profile("hooked").success is False
    state = state_manager.load()
    assert state.active_profile is None
    assert state.pre_stop_hooks == ["cleanup-before"]
    assert state.post_stop_hooks == ["cleanup-after"]

    engine.stop_all()
    assert hook_calls == ["fail-after-start", "cleanup-before", "cleanup-after"]
    assert "service-for-hook.service" not in mock_executor.active_system_units
