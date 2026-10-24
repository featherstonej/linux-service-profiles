"""Typer + Rich CLI entrypoint for Linux Service Profiles (srvp)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from srvp import __version__
from srvp.automation import (
    detect_port_conflicts,
    find_srvp_file,
    generate_power_udev_rule,
    generate_shell_hook,
)
from srvp.config import (
    ProfileConflictError,
    ProfilesFile,
    export_profiles_yaml,
    load_profiles,
    parse_imported_profiles,
    resolve_config_path,
    save_profiles,
    update_profiles,
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

    def __post_init__(self) -> None:
        if self.mock or os.environ.get("SRVP_MOCK") == "1":
            self.mock = True
            self.state_path = resolve_state_path(self.state_path, mock=True)

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
    effective_mock = mock or os.environ.get("SRVP_MOCK") == "1"
    cli_ctx = CliContext(
        config_path=resolve_config_path(config),
        state_path=resolve_state_path(state, mock=effective_mock),
        mock=effective_mock,
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
    try:
        state = StateManager(cli_ctx.state_path).load()
    except ValueError as exc:
        console.print(f"[bold red]State error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

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
        if prof.podman.containers or prof.podman.compose_stacks:
            extras.append("podman")
        if prof.libvirt.vms:
            extras.append("libvirt")
        if prof.android.adb_server:
            extras.append("adb")
        if prof.esp32_serial.enabled:
            extras.append("esp32-serial")
        extras_str = ", ".join(extras) if extras else "-"

        table.add_row(name, status_str, units_str, extras_str, prof.description)

    console.print(table)


def _render_transition_result(res: TransitionResult) -> None:
    mode_tag = "[bold yellow][DRY-RUN][/bold yellow] " if res.dry_run else ""
    target_label = escape(res.plan.target_profile or "None (Idle)")
    prev_label = escape(res.plan.previous_profile or "None")

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
            else (
                "[green]OK[/green]" if rec.success else f"[red]FAIL ({escape(rec.message)})[/red]"
            )
        )
        cmd_display = rec.command if rec.action != "check" else rec.message
        table.add_row(
            escape(rec.action),
            escape(rec.provider),
            escape(rec.identifier),
            escape(cmd_display),
            result_str,
        )

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
    try:
        res = engine.switch_profile(profile_name)
    except ValueError as exc:
        console.print(f"[bold red]State error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
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
    try:
        res = engine.stop_all()
    except ValueError as exc:
        console.print(f"[bold red]State error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
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
    try:
        state = state_mgr.load()
    except ValueError as exc:
        console.print(f"[bold red]State error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

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

    def _add_snapshot(current: ProfilesFile) -> None:
        if cleaned in current.profiles and not force:
            raise ProfileConflictError(f"Profile '{cleaned}' already exists.")
        current.profiles[cleaned] = snap_profile

    try:
        update_profiles(
            cli_ctx.config_path,
            _add_snapshot,
            initial=build_starter_profiles(),
        )
    except ProfileConflictError as exc:
        console.print(
            f"[bold yellow]{escape(str(exc))}[/bold yellow] Pass [bold]--force[/bold] to overwrite it."
        )
        raise typer.Exit(code=1) from exc
    saved_path = resolve_config_path(cli_ctx.config_path)

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
        typer.echo(rules_text, nl=not rules_text.endswith("\n"))


@app.command("auto")
def cmd_auto(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Directory to search upward from for a .srvp file."),
    ] = None,
    shell_hook: Annotated[
        str | None,
        typer.Option(
            "--shell-hook",
            help="Output shell prompt hook snippet for automatic directory switching (bash, zsh, fish).",
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress output when no switch is needed."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview transition without executing."),
    ] = False,
) -> None:
    """Automatically switch profiles based on a local `.srvp` file, or print a shell prompt hook."""
    if shell_hook:
        hook_text = generate_shell_hook(shell_hook)
        typer.echo(hook_text, nl=not hook_text.endswith("\n"))
        return

    cli_ctx: CliContext = ctx.obj
    found = find_srvp_file(directory)
    if found is None:
        if not quiet:
            console.print("[dim]No .srvp file found in current or parent directories.[/dim]")
        return

    srvp_path, target_profile = found
    profiles_file = cli_ctx.load_or_fail()
    if target_profile not in profiles_file.profiles:
        console.print(
            f"[bold red]Error:[/bold red] Profile '{target_profile}' (from {srvp_path}) is not defined in {cli_ctx.config_path}."
        )
        raise typer.Exit(code=1)

    state_mgr = StateManager(cli_ctx.state_path)
    current_active = state_mgr.load().active_profile
    if current_active == target_profile and not dry_run:
        if not quiet:
            console.print(
                f"[dim]Profile '{target_profile}' (from {srvp_path}) is already active.[/dim]"
            )
        return

    engine = ProfileEngine(
        config=profiles_file,
        state_manager=state_mgr,
        executor=cli_ctx.build_executor(override_dry_run=dry_run),
    )
    res = engine.switch_profile(target_profile)
    if not quiet or not res.success:
        _render_transition_result(res)
    if not res.success:
        raise typer.Exit(code=1)


@app.command("doctor")
def cmd_doctor(
    ctx: typer.Context,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Check port conflicts for a specific profile (checks all profiles if omitted).",
            autocompletion=complete_profile_names,
        ),
    ] = None,
) -> None:
    """Check for TCP port conflicts and configuration issues across profiles."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    executor = cli_ctx.build_executor()
    engine = ProfileEngine(
        config=profiles_file,
        state_manager=StateManager(cli_ctx.state_path),
        executor=executor,
    )

    warnings = engine.validate_config()
    target_names = [profile] if profile else list(profiles_file.profiles.keys())
    for name in target_names:
        if name not in profiles_file.profiles:
            console.print(f"[bold red]Error:[/bold red] Unknown profile '{name}'.")
            raise typer.Exit(code=1)

    table = Table(title="srvp Doctor — Port Conflict & Readiness Report")
    table.add_column("Profile", style="bold cyan")
    table.add_column("Status")
    table.add_column("Details")

    has_conflicts = False
    for name in target_names:
        prof = profiles_file.profiles[name]
        conflicts = detect_port_conflicts(prof, executor)
        if conflicts:
            has_conflicts = True
            detail_str = ", ".join(
                f":{c.port} required by {c.expected_for} is bound by {c.bound_by_process}"
                + (f" (PID {c.bound_by_pid})" if c.bound_by_pid else "")
                for c in conflicts
            )
            table.add_row(name, "[bold red]✗ PORT CONFLICT[/bold red]", detail_str)
        else:
            table.add_row(name, "[bold green]✓ READY[/bold green]", "No conflicting ports detected")

    console.print(table)
    if warnings:
        console.print("[bold yellow]Configuration warnings:[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]•[/yellow] {w}")

    if has_conflicts or warnings:
        raise typer.Exit(code=1)


@app.command("power-check")
def cmd_power_check(
    ctx: typer.Context,
    threshold: Annotated[
        int,
        typer.Option(
            "--threshold",
            "-t",
            help="Battery percentage threshold at or below which to switch when on battery.",
        ),
    ] = 20,
    target_profile: Annotated[
        str,
        typer.Option(
            "--target-profile",
            "-p",
            help="Profile to activate when unplugged and below threshold.",
            autocompletion=complete_profile_names,
        ),
    ] = "idle",
    udev_rule: Annotated[
        bool,
        typer.Option(
            "--udev-rule",
            help="Output a udev rule snippet for automatic power-triggered switching.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview transition without executing."),
    ] = False,
) -> None:
    """Check battery/AC power status and switch to the idle/battery-saver profile if below threshold."""
    if udev_rule:
        udev_text = generate_power_udev_rule(threshold=threshold, target_profile=target_profile)
        typer.echo(udev_text, nl=not udev_text.endswith("\n"))
        return

    cli_ctx: CliContext = ctx.obj
    executor = cli_ctx.build_executor(override_dry_run=dry_run)
    bat = executor.get_battery_status()
    if not bat.present:
        console.print("[dim]No laptop battery detected on this system.[/dim]")
        return

    power_str = "AC Plugged In" if bat.power_plugged else "On Battery"
    console.print(f"Battery Status: [bold]{bat.percent:.1f}%[/bold] ({power_str})")

    if not bat.power_plugged and bat.percent <= float(threshold):
        profiles_file = cli_ctx.load_or_fail()
        if target_profile not in profiles_file.profiles:
            console.print(
                f"[bold red]Error:[/bold red] Target profile '{target_profile}' not found."
            )
            raise typer.Exit(code=1)
        console.print(
            f"[bold yellow]Battery ({bat.percent:.1f}%) <= {threshold}% on battery power — "
            f"switching to '{target_profile}'...[/bold yellow]"
        )
        engine = ProfileEngine(
            config=profiles_file,
            state_manager=StateManager(cli_ctx.state_path),
            executor=executor,
        )
        res = engine.switch_profile(target_profile)
        _render_transition_result(res)
    else:
        console.print("[green]Power state above threshold; no profile switch needed.[/green]")


@app.command("export")
def cmd_export(
    ctx: typer.Context,
    profile_name: Annotated[
        str,
        typer.Argument(
            help="Name of the profile to export.",
            autocompletion=complete_profile_names,
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Optional YAML file path to write the exported profile."
        ),
    ] = None,
) -> None:
    """Export a profile definition to a standalone shareable YAML file or stdout."""
    cli_ctx: CliContext = ctx.obj
    profiles_file = cli_ctx.load_or_fail()
    if profile_name not in profiles_file.profiles:
        console.print(f"[bold red]Error:[/bold red] Profile '{profile_name}' not found.")
        raise typer.Exit(code=1)

    yaml_text = export_profiles_yaml(profiles_file, [profile_name])
    if output is not None:
        out_path = Path(output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_text, encoding="utf-8")
        console.print(f"[bold green]Exported profile '{profile_name}' to:[/bold green] {out_path}")
    else:
        typer.echo(yaml_text, nl=not yaml_text.endswith("\n"))


@app.command("import")
def cmd_import(
    ctx: typer.Context,
    file_path: Annotated[
        Path,
        typer.Argument(help="Path to the exported profile YAML file to import."),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing profiles with the same name."),
    ] = False,
) -> None:
    """Validate and merge a shareable profile YAML pack into profiles.yaml."""
    cli_ctx: CliContext = ctx.obj
    resolved_in = Path(file_path).expanduser().resolve()
    if not resolved_in.exists():
        console.print(f"[bold red]Error:[/bold red] Import file not found: {resolved_in}")
        raise typer.Exit(code=1)

    try:
        imported = parse_imported_profiles(resolved_in.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[bold red]Invalid profile import file:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    def _merge_imported(current: ProfilesFile) -> None:
        collisions = [name for name in imported.profiles if name in current.profiles]
        if collisions and not force:
            raise ProfileConflictError(f"Profile(s) {', '.join(collisions)} already exist.")
        for name, prof in imported.profiles.items():
            current.profiles[name] = prof

    try:
        update_profiles(
            cli_ctx.config_path,
            _merge_imported,
            initial=build_starter_profiles(),
        )
    except ProfileConflictError as exc:
        console.print(
            f"[bold yellow]{escape(str(exc))}[/bold yellow] Pass [bold]--force[/bold] to overwrite them."
        )
        raise typer.Exit(code=1) from exc

    saved = resolve_config_path(cli_ctx.config_path)
    console.print(
        f"[bold green]Imported {len(imported.profiles)} profile(s) "
        f"({', '.join(imported.profiles.keys())}) into:[/bold green] {saved}"
    )


def main() -> None:
    """Entrypoint for the `srvp` script."""
    app()


if __name__ == "__main__":
    main()
