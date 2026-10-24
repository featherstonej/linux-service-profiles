"""Headless Textual TUI tests verifying dashboard, switching, and profile editor."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from srvp.config import load_profiles
from srvp.executor import MockCommandExecutor
from srvp.tui.app import SrvpApp
from srvp.tui.editor import ProfileEditorModal


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
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
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
