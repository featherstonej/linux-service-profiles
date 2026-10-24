"""Headless Textual TUI tests verifying dashboard, switching, and profile editor."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from srvp.config import load_profiles
from srvp.executor import MockCommandExecutor
from srvp.tui.app import SrvpApp
from srvp.tui.editor import (
    ComposePickerModal,
    HookPickerModal,
    ProfileEditorModal,
    ServicePickerModal,
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

        # Populate inputs inside ProfileEditorModal
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
