"""Headless Textual TUI tests verifying dashboard, switching, and profile editor."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, RichLog

from srvp.config import (
    DockerComposeStack,
    Esp32SerialConfig,
    ProfileConfig,
    ProfilesFile,
    load_profiles,
    save_profiles,
    update_profiles,
)
from srvp.engine import TransitionPlan, TransitionResult
from srvp.executor import MockCommandExecutor
from srvp.providers import ActionRecord
from srvp.tui.app import SrvpApp
from srvp.tui.editor import (
    ComposePickerModal,
    HookPickerModal,
    ProfileEditorModal,
    ServicePickerModal,
    _parse_compose_stacks,
)


@pytest.mark.asyncio
async def test_tui_dashboard_switch_dry_run_and_stop(
    tmp_config_path: Path,
    tmp_state_path: Path,
    mock_executor: MockCommandExecutor,
) -> None:
    app = SrvpApp(
        config_path=tmp_config_path,
        state_path=tmp_state_path,
        executor=mock_executor,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        for btn_id in (
            "#btn-switch",
            "#btn-dry-run",
            "#btn-stop-all",
            "#btn-refresh",
            "#btn-new",
            "#btn-edit",
            "#btn-delete",
            "#btn-clone",
            "#btn-snapshot",
        ):
            btn = app.query_one(btn_id)
            assert btn.region.width >= 6, f"{btn_id} width too small: {btn.region}"
            assert btn.region.height == 3, f"{btn_id} height not 3: {btn.region}"
            assert btn.region.right <= sidebar.region.right, (
                f"{btn_id} clipped outside sidebar: {btn.region} vs {sidebar.region}"
            )

        table = app.query_one("#status-table", DataTable)
        assert table.row_count > 0

        # Switch to selected profile (web-db) using keybinding 's'
        await pilot.press("s")
        await pilot.pause()
        assert app.state_manager.load().active_profile == "web-db"
        assert "postgresql.service" in mock_executor.active_system_units

        # Trigger dry-run preview with 'd'
        await pilot.press("d")
        await pilot.pause()

        # Toggle auto-refresh pause/resume with 'p' and verify timer tick
        assert app.auto_refresh_enabled is True
        await pilot.press("p")
        await pilot.pause()
        assert app.auto_refresh_enabled is False
        await pilot.press("p")
        await pilot.pause()
        assert app.auto_refresh_enabled is True
        app._on_auto_refresh_tick()

        # Stop all with 'x'
        await pilot.press("x")
        await pilot.pause()
        assert app.state_manager.load().active_profile is None


@pytest.mark.asyncio
async def test_tui_profile_editor_create_and_delete(
    tmp_config_path: Path,
    tmp_state_path: Path,
    mock_executor: MockCommandExecutor,
) -> None:
    app = SrvpApp(
        config_path=tmp_config_path,
        state_path=tmp_state_path,
        executor=mock_executor,
    )
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()

        # Open "New Profile" modal via action method
        app.action_new_profile()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)

        # Open per-field Systemd System Units to Start picker
        app.screen._open_service_picker(
            input_id="#systemd-system",
            category="systemd_system",
            title="Select Systemd System Units to Start (Active)",
        )
        await pilot.pause()
        assert isinstance(app.screen, ServicePickerModal)

        # Verify all installed units are shown by default (Option B)
        visible_names = [it.name for it in app.screen._visible_items]
        assert "postgresql.service" in visible_names
        assert "systemd-journald.service" in visible_names

        # Toggle first visible row and apply to populate #systemd-system
        app.screen.action_toggle_selected()
        app.screen._apply_and_close()
        await pilot.pause()

        assert isinstance(app.screen, ProfileEditorModal)
        assert app.screen.query_one("#systemd-system", Input).value != ""

        # Open per-field Systemd Units to Ensure Stopped picker
        app.screen._open_service_picker(
            input_id="#systemd-stopped",
            category="systemd_system",
            title="Select Systemd Units to Ensure Stopped (Inactive)",
        )
        await pilot.pause()
        assert isinstance(app.screen, ServicePickerModal)
        app.screen.action_toggle_selected()
        app.screen._apply_and_close()
        await pilot.pause()

        assert isinstance(app.screen, ProfileEditorModal)
        assert app.screen.query_one("#systemd-stopped", Input).value != ""

        # Open Docker Compose Picker Modal and select first discovered compose file
        app.screen._open_compose_picker()
        await pilot.pause()
        assert isinstance(app.screen, ComposePickerModal)
        app.screen.action_toggle_selected()
        await pilot.click("#btn-compose-apply")
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        assert "webapp-stack:" in app.screen.query_one("#docker-compose", Input).value

        # Open Post-Start Hook Picker Modal and select first preset hook
        app.screen._open_hook_picker("post_start")
        await pilot.pause()
        assert isinstance(app.screen, HookPickerModal)
        app.screen.action_toggle_selected()
        await pilot.click("#btn-hook-apply")
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        assert app.screen.query_one("#hooks-post-start", Input).value != ""

        # Populate inputs inside ProfileEditorModal; first verify duplicate name is rejected
        app.screen.query_one("#profile-name", Input).value = "web-db"
        app.screen._validate_and_save()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)

        app.screen.query_one("#profile-name", Input).value = "custom-iot"
        app.screen.query_one("#profile-desc", Input).value = "Custom IoT MQTT stack"
        app.screen.query_one("#systemd-system", Input).value = "mosquitto, postgresql"
        app.screen._validate_and_save()
        await pilot.pause()

        # Verify new profile was persisted to profiles.yaml
        reloaded = load_profiles(tmp_config_path)
        assert "custom-iot" in reloaded.profiles
        assert reloaded.profiles["custom-iot"].systemd.system == [
            "mosquitto.service",
            "postgresql.service",
        ]

        # Test deleting the newly created profile
        app.selected_profile = "custom-iot"
        app.action_delete_selected()
        await pilot.pause()
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()

        reloaded_after = load_profiles(tmp_config_path)
        assert "custom-iot" not in reloaded_after.profiles

        # Test Clone Profile ('c' / action_clone_selected)
        app.selected_profile = "web-db"
        app.action_clone_selected()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        assert app.screen.query_one("#profile-name", Input).value == "web-db-copy"
        app.screen._validate_and_save()
        await pilot.pause()
        assert "web-db-copy" in load_profiles(tmp_config_path).profiles

        # Verify editing docker-dev preserves semicolon inside post_start hook losslessly,
        # preserves Compose stack project_dir & services through both picker and editor,
        # and preserves custom ESP32 overrides (check_dialout_group, stop_conflicting_services, ports_glob).
        app.profiles_file.profiles["docker-dev"].docker.compose_stacks = [
            DockerComposeStack(
                name="api",
                file="/tmp/proj/docker-compose.yml",
                project_dir="/tmp/proj",
                services=["web", "worker"],
            ),
            DockerComposeStack(
                name="metrics",
                file="/tmp/proj/docker-compose.yml",
                project_dir="/tmp/proj",
                services=["prometheus"],
            ),
        ]
        app.profiles_file.profiles["docker-dev"].esp32_serial = Esp32SerialConfig(
            enabled=False,
            check_dialout_group=False,
            stop_conflicting_services=False,
            ports_glob=["/dev/ttyACM0"],
        )
        save_profiles(app.profiles_file, tmp_config_path)

        app.selected_profile = "docker-dev"
        app.action_edit_selected()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)

        # Open ComposePickerModal and click Apply without losing project_dir, services, or duplicate-file stacks
        app.screen._open_compose_picker()
        await pilot.pause()
        assert isinstance(app.screen, ComposePickerModal)
        await pilot.click("#btn-compose-apply")
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)

        app.screen._validate_and_save()
        await pilot.pause()
        saved_docker_dev = load_profiles(tmp_config_path).profiles["docker-dev"]
        assert saved_docker_dev.hooks.post_start == [
            "echo 'Docker daemon active; local host DB daemons stopped to avoid port clashes.'"
        ]
        assert len(saved_docker_dev.docker.compose_stacks) == 2
        assert saved_docker_dev.docker.compose_stacks[0].project_dir == "/tmp/proj"
        assert saved_docker_dev.docker.compose_stacks[0].services == ["web", "worker"]
        assert saved_docker_dev.docker.compose_stacks[1].name == "metrics"
        assert saved_docker_dev.docker.compose_stacks[1].services == ["prometheus"]
        assert saved_docker_dev.esp32_serial.check_dialout_group is False
        assert saved_docker_dev.esp32_serial.stop_conflicting_services is False
        assert saved_docker_dev.esp32_serial.ports_glob == ["/dev/ttyACM0"]

        # Activate web-db-copy, verify active profile cannot be deleted while active,
        # and verify renaming an active profile migrates state.active_profile
        app.selected_profile = "web-db-copy"
        app.action_switch_selected()
        await pilot.pause()
        assert app.state_manager.load().active_profile == "web-db-copy"

        app.action_delete_selected()
        await pilot.pause()
        assert "web-db-copy" in load_profiles(tmp_config_path).profiles

        app.action_edit_selected()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        # First verify rename collision against an existing profile is rejected
        app.screen.query_one("#profile-name", Input).value = "docker-dev"
        app.screen._validate_and_save()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)

        app.screen.query_one("#profile-name", Input).value = "web-db-renamed"
        app.screen._validate_and_save()
        await pilot.pause()
        assert app.state_manager.load().active_profile == "web-db-renamed"

        # Test Snapshot Running ('m' / action_snapshot_running)
        mock_executor.active_system_units.add("postgresql.service")
        app.action_snapshot_running()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        assert app.screen.query_one("#profile-name", Input).value == "snapshot-profile"
        assert "postgresql.service" in app.screen.query_one("#systemd-system", Input).value
        app.screen._validate_and_save()
        await pilot.pause()
        assert "snapshot-profile" in load_profiles(tmp_config_path).profiles


@pytest.mark.asyncio
async def test_tui_writes_preserve_concurrent_config_changes(
    tmp_config_path: Path,
    tmp_state_path: Path,
    mock_executor: MockCommandExecutor,
) -> None:
    app = SrvpApp(
        config_path=tmp_config_path,
        state_path=tmp_state_path,
        executor=mock_executor,
    )
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()

        update_profiles(
            tmp_config_path,
            lambda current: current.profiles.__setitem__(
                "created-externally-before-new", ProfileConfig()
            ),
        )
        app._on_profile_saved_new(("created-in-tui", ProfileConfig()))
        saved = load_profiles(tmp_config_path)
        assert "created-externally-before-new" in saved.profiles
        assert "created-in-tui" in saved.profiles

        app.selected_profile = "docker-dev"
        app.action_edit_selected()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        app.screen.query_one("#profile-desc", Input).value = "Updated in TUI"
        update_profiles(
            tmp_config_path,
            lambda current: current.profiles.__setitem__(
                "created-externally-before-edit", ProfileConfig()
            ),
        )
        app.screen._validate_and_save()
        await pilot.pause()
        saved = load_profiles(tmp_config_path)
        assert "created-externally-before-edit" in saved.profiles
        assert saved.profiles["docker-dev"].description == "Updated in TUI"

        app.selected_profile = "docker-dev"
        app.action_edit_selected()
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditorModal)
        app.screen.query_one("#profile-desc", Input).value = "Stale TUI edit"

        def _change_docker_dev(current: ProfilesFile) -> None:
            current.profiles["docker-dev"].description = "External concurrent edit"

        update_profiles(tmp_config_path, _change_docker_dev)
        app.screen._validate_and_save()
        await pilot.pause()
        saved = load_profiles(tmp_config_path)
        assert saved.profiles["docker-dev"].description == "External concurrent edit"

        app.selected_profile = "created-in-tui"
        app.action_delete_selected()
        await pilot.pause()
        update_profiles(
            tmp_config_path,
            lambda current: current.profiles.__setitem__(
                "created-externally-before-delete", ProfileConfig()
            ),
        )
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()
        saved = load_profiles(tmp_config_path)
        assert "created-in-tui" not in saved.profiles
        assert "created-externally-before-delete" in saved.profiles


def test_compose_editor_does_not_copy_metadata_by_file_only() -> None:
    existing = [
        DockerComposeStack(
            name="api",
            file="/tmp/project/compose.yml",
            project_dir="/tmp/project",
            services=["web", "worker"],
        ),
        DockerComposeStack(
            name="metrics",
            file="/tmp/project/compose.yml",
            project_dir="/tmp/project",
            services=["prometheus"],
        ),
    ]

    parsed = _parse_compose_stacks(
        "api:/tmp/project/compose.yml, "
        "metrics:/tmp/project/compose.yml, "
        "new-stack:/tmp/project/compose.yml",
        existing,
    )
    assert parsed[-1].project_dir is None
    assert parsed[-1].services == []

    with pytest.raises(ValueError, match="Ambiguous Compose entry"):
        _parse_compose_stacks(
            "api:/tmp/project/compose.yml, renamed:/tmp/project/compose.yml",
            existing,
        )


@pytest.mark.asyncio
async def test_profile_editor_fits_80_column_terminal(
    tmp_config_path: Path,
    tmp_state_path: Path,
    mock_executor: MockCommandExecutor,
) -> None:
    app = SrvpApp(
        config_path=tmp_config_path,
        state_path=tmp_state_path,
        executor=mock_executor,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        editor = ProfileEditorModal("narrow", ProfileConfig())
        app.push_screen(editor)
        await pilot.pause()
        dialog = editor.query_one("#editor-dialog")
        assert dialog.region.width <= 80
        assert dialog.region.right <= 80


@pytest.mark.asyncio
async def test_transition_log_escapes_dynamic_markup(
    tmp_config_path: Path,
    tmp_state_path: Path,
    mock_executor: MockCommandExecutor,
) -> None:
    app = SrvpApp(
        config_path=tmp_config_path,
        state_path=tmp_state_path,
        executor=mock_executor,
    )
    result = TransitionResult(
        plan=TransitionPlan(previous_profile=None, target_profile=None),
        records=[
            ActionRecord(
                action="start[/red]",
                provider="provider",
                identifier="service[/red]",
                command="echo '[/red]'",
                success=False,
                message="failure [/red]",
            )
        ],
        dry_run=False,
    )
    async with app.run_test() as pilot:
        log = app.query_one("#action-log", RichLog)
        app._write_transition_records(log, result)
        await pilot.pause()
