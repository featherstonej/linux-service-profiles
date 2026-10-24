"""Typer + Rich CLI entrypoint for Linux Service Profiles (srvp)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from srvp import __version__
from srvp.config import (
    ProfilesFile,
    load_profiles,
    resolve_config_path,
    save_profiles,
)
from srvp.discovery import build_snapshot_profile
from srvp.engine import ProfileEngine, TransitionResult
from srvp.executor import CommandExecutor, MockCommandExecutor, RealCommandExecutor
from srvp.polkit import generate_polkit_rules
from srvp.state import StateManager, resolve_state_path
from srvp.templates import build_starter_profiles
from srvp.tui.app import SrvpApp

app = typer.Typer(
    name="srvp",
    help="Linux Service Profiles (srvp) — Switch and customize Linux service profiles.",
    no_args_is_help=False,
    add_completion=True,
)
console = Console()


def complete_profile_names(ctx: typer.Context, incomplete: str) -> list[str]:
    """Dynamic shell tab-completion for configured profile names."""
    try:
        cfg_param = ctx.params.get("config") if ctx and ctx.params else None
        if cfg_param is None and ctx.parent and ctx.parent.params:
            cfg_param = ctx.parent.params.get("config")
        profiles_file = load_profiles(resolve_config_path(cfg_param))
        return [name for name in profiles_file.profiles if name.startswith(incomplete)]
    except Exception:
        return []


@dataclass
class CliContext:
    """Shared context populated by the root Typer callback."""

    config_path: Path
    state_path: Path
    mock: bool
    dry_run: bool

    def build_executor(self, override_dry_run: bool = False) -> CommandExecutor:
        effective_dry = self.dry_run or override_dry_run
        if self.mock or os.environ.get("SRVP_MOCK") == "1":
            mock_state_file = self.state_path.parent / "mock_system.json"
            return MockCommandExecutor(dry_run=effective_dry, state_file=mock_state_file)
        return RealCommandExecutor(dry_run=effective_dry)

    def load_or_fail(self) -> ProfilesFile:
        try:
            return load_profiles(self.config_path)
        except FileNotFoundError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            console.print(f"[bold red]Invalid configuration:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"srvp (linux-service-profiles) version {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to profiles.yaml configuration file."),
    ] = None,
    state: Annotated[
        Path | None,
        typer.Option("--state", "-s", help="Path to state.json file."),
    ] = None,
    mock: Annotated[
        bool,
        typer.Option(
            "--mock",
            help="Use deterministic MockCommandExecutor instead of mutating host services.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Preview commands that would be executed without making changes.",
        ),
    ] = False,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-v",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    """Manage Linux development service profiles via CLI or interactive TUI."""
    cli_ctx = CliContext(
        config_path=resolve_config_path(config),
        state_path=resolve_state_path(state),
        mock=mock,
        dry_run=dry_run,
    )
    ctx.obj = cli_ctx

    if ctx.invoked_subcommand is None:
        # Default to launching the interactive Textual TUI when invoked with no subcommand
        tui_app = SrvpApp(
            config_path=cli_ctx.config_path,
            state_path=cli_ctx.state_path,
            executor=cli_ctx.build_executor(),
        )
        tui_app.run()


@app.command("ui")
def cmd_ui(ctx: typer.Context) -> None:
    """Launch the interactive Textual Terminal UI (dashboard & profile editor)."""
    cli_ctx: CliContext = ctx.obj
    tui_app = SrvpApp(
        config_path=cli_ctx.config_path,
        state_path=cli_ctx.state_path,
        executor=cli_ctx.build_executor(),
    )
    tui_app.run()


@app.command("init")
def cmd_init(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing profiles.yaml if present."),
    ] = False,
) -> None:
    """Generate a starter profiles.yaml with templates (web-db, docker-dev, android-dev, esp32-embedded, idle)."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.config_path.exists() and not force:
        console.print(
            f"[yellow]Configuration already exists at[/yellow] {cli_ctx.config_path}\n"
            "Use [bold]srvp init --force[/bold] to overwrite it."
        )
        raise typer.Exit(code=1)

    starter = build_starter_profiles()
    saved_path = save_profiles(starter, cli_ctx.config_path)
    console.print(
        Panel(
            f"Created starter profiles at [bold cyan]{saved_path}[/bold cyan]\n"
            f"Included profiles: [bold]{', '.join(starter.profiles.keys())}[/bold]",
            title="srvp Initialized",
            border_style="green",
        )
    )


@app.command("list")
def cmd_list(ctx: typer.Context) -> None:
    """List all configured profiles and highlight the currently active profile."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    state = StateManager(cli_ctx.state_path).load()

    table = Table(title="Configured Linux Service Profiles")
    table.add_column("Profile", style="bold cyan")
    table.add_column("Status")
    table.add_column("Systemd Units")
    table.add_column("Extras (Docker / ADB / ESP32)")
    table.add_column("Description")

    for name, prof in profiles_file.profiles.items():
        is_active = name == state.active_profile
        status_str = "[bold green]● ACTIVE[/bold green]" if is_active else "[dim]inactive[/dim]"
        units = prof.effective_system_units() + [f"user:{u}" for u in prof.systemd.user]
        units_str = ", ".join(units) if units else "-"

        extras: list[str] = []
        if prof.docker.enabled or prof.docker.containers or prof.docker.compose_stacks:
            extras.append("docker")
        if prof.android.adb_server:
            extras.append("adb")
        if prof.esp32_serial.enabled:
            extras.append("esp32-serial")
        extras_str = ", ".join(extras) if extras else "-"

        table.add_row(name, status_str, units_str, extras_str, prof.description)

    console.print(table)


def _render_transition_result(res: TransitionResult) -> None:
    mode_tag = "[bold yellow][DRY-RUN][/bold yellow] " if res.dry_run else ""
    target_label = res.plan.target_profile or "None (Idle)"
    prev_label = res.plan.previous_profile or "None"

    console.print(
        f"{mode_tag}Transition: [bold]{prev_label}[/bold] -> [bold cyan]{target_label}[/bold cyan]"
    )
    if not res.records:
        console.print("  [dim]No service actions needed.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Action")
    table.add_column("Provider")
    table.add_column("Target")
    table.add_column("Command / Detail")
    table.add_column("Result")

    for rec in res.records:
        result_str = (
            "[yellow]DRY-RUN[/yellow]"
            if rec.dry_run
            else ("[green]OK[/green]" if rec.success else f"[red]FAIL ({rec.message})[/red]")
        )
        cmd_display = rec.command if rec.action != "check" else rec.message
        table.add_row(rec.action, rec.provider, rec.identifier, cmd_display, result_str)

    console.print(table)


@app.command("switch")
def cmd_switch(
    ctx: typer.Context,
    profile_name: Annotated[
        str,
        typer.Argument(
            help="Name of the profile to activate.",
            autocompletion=complete_profile_names,
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview transition plan without executing."),
    ] = False,
) -> None:
    """Perform an exclusive switch to <profile_name>, stopping unneeded services first."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    if profile_name not in profiles_file.profiles:
        console.print(
            f"[bold red]Error:[/bold red] Profile '{profile_name}' does not exist. "
            f"Available: {', '.join(profiles_file.profiles.keys())}"
        )
        raise typer.Exit(code=1)

    engine = ProfileEngine(
        config=profiles_file,
        state_manager=StateManager(cli_ctx.state_path),
        executor=cli_ctx.build_executor(override_dry_run=dry_run),
    )
    res = engine.switch_profile(profile_name)
    _render_transition_result(res)
    if not res.success:
        raise typer.Exit(code=1)


@app.command("stop")
def cmd_stop(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview stop actions without executing."),
    ] = False,
) -> None:
    """Deactivate the currently active profile and stop all services it started."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    engine = ProfileEngine(
        config=profiles_file,
        state_manager=StateManager(cli_ctx.state_path),
        executor=cli_ctx.build_executor(override_dry_run=dry_run),
    )
    res = engine.stop_all()
    _render_transition_result(res)
    if not res.success:
        raise typer.Exit(code=1)


@app.command("status")
def cmd_status(
    ctx: typer.Context,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Inspect services for a specific profile.",
            autocompletion=complete_profile_names,
        ),
    ] = None,
) -> None:
    """Display active profile state, live service statuses, and CPU/RAM usage."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    state_mgr = StateManager(cli_ctx.state_path)
    state = state_mgr.load()

    engine = ProfileEngine(
        config=profiles_file,
        state_manager=state_mgr,
        executor=cli_ctx.build_executor(),
    )

    active_title = state.active_profile or "None (Idle)"
    console.print(
        Panel(
            f"Active Profile: [bold green]{active_title}[/bold green]\n"
            f"Activated At:   {state.activated_at or '-'}\n"
            f"Config Path:    {cli_ctx.config_path}",
            title="srvp Status",
            border_style="cyan",
        )
    )

    statuses = engine.get_profile_statuses(profile)
    table = Table(title="Live Service & Tool Telemetry")
    table.add_column("Provider", style="cyan")
    table.add_column("Service / Item", style="bold")
    table.add_column("State")
    table.add_column("PID", justify="right")
    table.add_column("CPU %", justify="right")
    table.add_column("RAM (MB)", justify="right")
    table.add_column("Details")

    for st in statuses:
        state_badge = (
            f"[bold green]● {st.status_text}[/bold green]"
            if st.active
            else f"[dim]○ {st.status_text}[/dim]"
        )
        pid_str = str(st.pid) if st.pid else "-"
        cpu_str = f"{st.cpu_percent:.1f}%" if st.pid else "-"
        mem_str = f"{st.memory_mb:.1f}" if st.pid else "-"
        table.add_row(st.provider, st.identifier, state_badge, pid_str, cpu_str, mem_str, st.detail)

    console.print(table)


@app.command("validate")
def cmd_validate(ctx: typer.Context) -> None:
    """Validate profiles.yaml syntax and check for conflicting units or missing compose files."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    engine = ProfileEngine(
        config=profiles_file,
        state_manager=StateManager(cli_ctx.state_path),
        executor=cli_ctx.build_executor(),
    )
    warnings = engine.validate_config()
    if warnings:
        console.print("[bold yellow]Configuration warnings found:[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]•[/yellow] {w}")
        raise typer.Exit(code=1)

    console.print(
        f"[bold green]✓ Configuration is valid[/bold green] "
        f"({len(profiles_file.profiles)} profiles verified in {cli_ctx.config_path})."
    )


@app.command("snapshot")
def cmd_snapshot(
    ctx: typer.Context,
    profile_name: Annotated[
        str,
        typer.Argument(help="Name of the new profile to create from currently running services."),
    ],
    description: Annotated[
        str,
        typer.Option(
            "--description",
            "-d",
            help="Description for the captured snapshot profile.",
        ),
    ] = "Snapshot of running machine services and containers.",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite if a profile with this name already exists."),
    ] = False,
) -> None:
    """Capture currently running non-core systemd services, containers, and ADB into a new profile."""
    cli_ctx: CliContext = ctx.obj
    cleaned = profile_name.strip()
    if not cleaned or " " in cleaned:
        console.print(
            "[bold red]Error:[/bold red] Profile name must be non-empty and contain no spaces."
        )
        raise typer.Exit(code=1)

    if cli_ctx.config_path.exists():
        profiles_file = cli_ctx.load_or_fail()
    else:
        profiles_file = build_starter_profiles()

    if cleaned in profiles_file.profiles and not force:
        console.print(
            f"[bold yellow]Profile '{cleaned}' already exists.[/bold yellow] "
            "Pass [bold]--force[/bold] to overwrite it."
        )
        raise typer.Exit(code=1)

    executor = cli_ctx.build_executor()
    snap_profile = build_snapshot_profile(executor, description=description)
    profiles_file.profiles[cleaned] = snap_profile
    saved_path = save_profiles(profiles_file, cli_ctx.config_path)

    sys_str = ", ".join(snap_profile.systemd.system) or "none"
    usr_str = ", ".join(snap_profile.systemd.user) or "none"
    cnt_str = ", ".join(snap_profile.docker.containers) or "none"
    console.print(
        Panel(
            f"Saved snapshot profile [bold cyan]{cleaned}[/bold cyan] to {saved_path}\n"
            f"System Units:      [green]{sys_str}[/green]\n"
            f"User Units:        [green]{usr_str}[/green]\n"
            f"Docker Containers: [green]{cnt_str}[/green]\n"
            f"ADB Server:        {'active' if snap_profile.android.adb_server else 'inactive'}",
            title="srvp Snapshot Captured",
            border_style="green",
        )
    )


@app.command("polkit-setup")
def cmd_polkit_setup(
    ctx: typer.Context,
    group: Annotated[
        str | None,
        typer.Option(
            "--group",
            "-g",
            help="Unix group granted passwordless systemctl access (defaults to both 'sudo' and 'wheel').",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional file path to write the Polkit rules."),
    ] = None,
) -> None:
    """Generate a Polkit rule file allowing passwordless systemctl switching for profile units."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    rules_text = generate_polkit_rules(profiles_file, group=group)

    if output is not None:
        out_path = Path(output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rules_text, encoding="utf-8")
        console.print(f"[bold green]Wrote Polkit rules to:[/bold green] {out_path}")
    else:
        console.print(rules_text)


def main() -> None:
    """Entrypoint for the `srvp` script."""
    app()


if __name__ == "__main__":
    main()
