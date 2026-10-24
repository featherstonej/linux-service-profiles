"""End-to-end CLI tests using Typer's CliRunner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from srvp.cli import _render_transition_result, app, complete_profile_names
from srvp.config import load_profiles
from srvp.engine import TransitionPlan, TransitionResult
from srvp.providers import ActionRecord

runner = CliRunner()


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "srvp (linux-service-profiles) version" in result.stdout


def test_cli_init_list_switch_status_stop_validate_polkit(tmp_path: Path) -> None:
    cfg = tmp_path / "profiles.yaml"
    st = tmp_path / "state.json"
    polkit_out = tmp_path / "50-srvp.rules"
    base_args = ["--config", str(cfg), "--state", str(st), "--mock"]

    # 1. init
    r_init = runner.invoke(app, [*base_args, "init"])
    assert r_init.exit_code == 0
    assert cfg.exists()

    # Second init without --force should fail
    r_init_dup = runner.invoke(app, [*base_args, "init"])
    assert r_init_dup.exit_code == 1

    # 2. list
    r_list = runner.invoke(app, [*base_args, "list"])
    assert r_list.exit_code == 0
    assert "web-db" in r_list.stdout
    assert "esp32-embedded" in r_list.stdout

    # 3. switch web-db
    r_switch = runner.invoke(app, [*base_args, "switch", "web-db"])
    assert r_switch.exit_code == 0
    assert "postgresql.service" in r_switch.stdout

    # 4. status
    r_status = runner.invoke(app, [*base_args, "status"])
    assert r_status.exit_code == 0
    assert "web-db" in r_status.stdout

    # 4b. snapshot currently running services (postgresql + redis-server) into a new profile
    r_snap = runner.invoke(
        app,
        [*base_args, "snapshot", "captured-dev", "--description", "Captured active dev stack"],
    )
    assert r_snap.exit_code == 0
    assert "captured-dev" in r_snap.stdout
    loaded_after_snap = load_profiles(cfg)
    assert "captured-dev" in loaded_after_snap.profiles
    assert "postgresql.service" in loaded_after_snap.profiles["captured-dev"].systemd.system

    # 4c. verify dynamic shell tab-completion helper
    fake_ctx = SimpleNamespace(params={"config": cfg}, parent=None)
    completions = complete_profile_names(fake_ctx, "web")  # type: ignore[arg-type]
    assert completions == ["web-db"]

    # 5. dry-run switch to docker-dev
    r_dry = runner.invoke(app, [*base_args, "switch", "docker-dev", "--dry-run"])
    assert r_dry.exit_code == 0
    assert "DRY-RUN" in r_dry.stdout

    # 6. stop
    r_stop = runner.invoke(app, [*base_args, "stop"])
    assert r_stop.exit_code == 0
    assert "None (Idle)" in r_stop.stdout

    # 7. validate
    r_val = runner.invoke(app, [*base_args, "validate"])
    assert r_val.exit_code == 0
    assert "Configuration is valid" in r_val.stdout

    # 8. polkit-setup (--output and stdout verbatim without line-wrapping)
    r_pk = runner.invoke(app, [*base_args, "polkit-setup", "--output", str(polkit_out)])
    assert r_pk.exit_code == 0
    assert polkit_out.exists()
    file_rules = polkit_out.read_text(encoding="utf-8")
    assert "org.freedesktop.systemd1.manage-units" in file_rules

    r_pk_stdout = runner.invoke(app, [*base_args, "polkit-setup"])
    assert r_pk_stdout.exit_code == 0
    assert r_pk_stdout.stdout == file_rules


def test_cli_mock_isolates_activation_state_from_real_state(tmp_path: Path) -> None:
    from srvp.state import ManagedItem, StateManager

    cfg = tmp_path / "profiles.yaml"
    real_st = tmp_path / "state.json"
    base_args = ["--config", str(cfg), "--state", str(real_st), "--mock"]

    runner.invoke(app, [*base_args, "init"])

    # Record a real activation in state.json
    real_mgr = StateManager(real_st, mock=False)
    real_mgr.record_activation(
        "web-db",
        [ManagedItem(provider="systemd_system", identifier="postgresql.service")],
    )

    # Run mock switch and mock stop; real state.json must remain untouched
    r_sw = runner.invoke(app, [*base_args, "switch", "docker-dev"])
    assert r_sw.exit_code == 0
    assert (tmp_path / "mock_state.json").exists()
    assert real_mgr.load().active_profile == "web-db"

    r_stop = runner.invoke(app, [*base_args, "stop"])
    assert r_stop.exit_code == 0
    assert real_mgr.load().active_profile == "web-db"
    assert len(real_mgr.load().active_items) == 1


def test_transition_result_escapes_untrusted_rich_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = Console(record=True, width=160)
    monkeypatch.setattr("srvp.cli.console", output)
    result = TransitionResult(
        plan=TransitionPlan(
            previous_profile=None,
            target_profile="profile[/red]",
        ),
        records=[
            ActionRecord(
                action="hook[/red]",
                provider="provider[/red]",
                identifier="unit[/red]",
                command="echo '[/red]'",
                success=False,
                message="error [/red]",
            )
        ],
        dry_run=False,
    )

    _render_transition_result(result)
    rendered = output.export_text()
    assert "echo '[/red]'" in rendered
    assert "error [/red]" in rendered
    assert "unit[/red]" in rendered
