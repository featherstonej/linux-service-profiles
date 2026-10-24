"""Main Textual TUI application for Linux Service Profiles (srvp)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from srvp.config import ProfileConfig, load_profiles, save_profiles
from srvp.engine import ProfileEngine
from srvp.executor import CommandExecutor, MockCommandExecutor, RealCommandExecutor
from srvp.state import StateManager
from srvp.templates import build_starter_profiles
from srvp.tui.editor import ConfirmDeleteModal, ProfileEditorModal


class ProfileListItem(ListItem):
    """ListItem storing its associated profile_name."""

    def __init__(self, profile_name: str, is_active: bool) -> None:
        super().__init__()
        self.profile_name = profile_name
        self.is_active = is_active

    def compose(self) -> ComposeResult:
        badge = " [bold green]● ACTIVE[/bold green]" if self.is_active else ""
        yield Label(f"{self.profile_name}{badge}")


class SrvpApp(App[None]):
    """Interactive Terminal UI for switching, monitoring, and editing Linux service profiles."""

    TITLE = "srvp — Linux Service Profiles"
    SUB_TITLE = "Exclusive Service Switcher & Live Dashboard"

    CSS = """
    #main-layout {
        height: 1fr;
    }
    #sidebar {
        width: 38;
        border-right: solid $primary;
        padding: 1;
    }
    #active-banner {
        background: $boost;
        padding: 0 1;
        margin-bottom: 1;
        text-style: bold;
    }
    #profile-list {
        height: 1fr;
        min-height: 6;
        border: round $surface-lighten-2;
        margin-bottom: 1;
    }
    .action-row {
        width: 100%;
        height: 3;
        margin-bottom: 0;
    }
    .action-row Button {
        width: 1fr;
        min-width: 6;
        padding: 0;
        margin-right: 1;
    }
    #main-pane {
        width: 1fr;
        padding: 1;
    }
    #profile-details {
        height: 8;
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
    }
    #status-table {
        height: 1fr;
        border: round $primary;
        margin-bottom: 1;
    }
    #action-log {
        height: 9;
        border: round $surface-lighten-2;
    }
    """

    BINDINGS = [
        Binding("s", "switch_selected", "Switch Profile"),
        Binding("d", "dry_run_selected", "Dry-Run Preview"),
        Binding("x", "stop_all", "Stop All (Idle)"),
        Binding("n", "new_profile", "New Profile"),
        Binding("e", "edit_selected", "Edit Profile"),
        Binding("delete", "delete_selected", "Delete Profile"),
        Binding("r", "refresh_dashboard", "Refresh Status"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        executor: CommandExecutor,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.state_manager = StateManager(state_path)
        self.executor = executor

        if not self.config_path.exists():
            save_profiles(build_starter_profiles(), self.config_path)

        self.profiles_file = load_profiles(self.config_path)
        self.engine = ProfileEngine(self.profiles_file, self.state_manager, self.executor)
        first_names = list(self.profiles_file.profiles.keys())
        self.selected_profile: str | None = first_names[0] if first_names else None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                yield Static("Active: None", id="active-banner")
                yield ListView(id="profile-list")
                with Horizontal(classes="action-row"):
                    yield Button("Switch (s)", id="btn-switch", variant="success")
                    yield Button("Dry-Run (d)", id="btn-dry-run", variant="primary")
                with Horizontal(classes="action-row"):
                    yield Button("Stop All (x)", id="btn-stop-all", variant="warning")
                    yield Button("Refresh (r)", id="btn-refresh")
                with Horizontal(classes="action-row"):
                    yield Button("New (n)", id="btn-new")
                    yield Button("Edit (e)", id="btn-edit")
                    yield Button("Del", id="btn-delete", variant="error")
            with Vertical(id="main-pane"):
                yield Static("Select a profile to view details.", id="profile-details")
                yield DataTable(id="status-table")
                yield RichLog(id="action-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#status-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Provider", "Service / Item", "Status", "PID", "CPU %", "RAM (MB)", "Details"
        )
        log = self.query_one("#action-log", RichLog)
        log.write(f"[bold cyan]Loaded configuration:[/bold cyan] {self.config_path}")
        self._rebuild_profile_list()
        self.refresh_views()

    def _rebuild_profile_list(self) -> None:
        state = self.state_manager.load()
        active_name = state.active_profile
        banner = self.query_one("#active-banner", Static)
        banner.update(
            f"Active: [bold green]{active_name}[/bold green]"
            if active_name
            else "Active: [dim]None (Idle)[/dim]"
        )

        list_view = self.query_one("#profile-list", ListView)
        list_view.clear()
        names = list(self.profiles_file.profiles.keys())
        if self.selected_profile not in names:
            self.selected_profile = names[0] if names else None

        selected_idx = 0
        for idx, name in enumerate(names):
            list_view.append(ProfileListItem(name, is_active=(name == active_name)))
            if name == self.selected_profile:
                selected_idx = idx
        if names:
            list_view.index = selected_idx

    def refresh_views(self) -> None:
        """Update the profile summary and live service status table."""
        details = self.query_one("#profile-details", Static)
        if self.selected_profile and self.selected_profile in self.profiles_file.profiles:
            prof = self.profiles_file.profiles[self.selected_profile]
            plan = self.engine.compute_transition_plan(self.selected_profile)
            to_start = ", ".join(it.identifier for it in plan.items_to_start) or "none"
            to_stop = ", ".join(it.identifier for it in plan.items_to_stop) or "none"
            details.update(
                f"[bold]{self.selected_profile}[/bold] — {prof.description}\n"
                f"[green]Will Ensure Active:[/green] {to_start}\n"
                f"[yellow]Will Ensure Stopped:[/yellow] {to_stop}\n"
                f"[cyan]Hooks:[/cyan] {len(prof.hooks.pre_start)} pre-start, "
                f"{len(prof.hooks.post_start)} post-start"
            )
        else:
            details.update("No profile selected.")

        table = self.query_one("#status-table", DataTable)
        table.clear()
        statuses = self.engine.get_profile_statuses(self.selected_profile)
        for st in statuses:
            status_cell = (
                f"[bold green]● {st.status_text}[/bold green]"
                if st.active
                else f"[dim]○ {st.status_text}[/dim]"
            )
            pid_str = str(st.pid) if st.pid else "-"
            cpu_str = f"{st.cpu_percent:.1f}%" if st.pid else "-"
            mem_str = f"{st.memory_mb:.1f}" if st.pid else "-"
            table.add_row(
                st.provider,
                st.identifier,
                status_cell,
                pid_str,
                cpu_str,
                mem_str,
                st.detail,
            )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ProfileListItem):
            self.selected_profile = event.item.profile_name
            self.refresh_views()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ProfileListItem):
            self.selected_profile = event.item.profile_name
            self.refresh_views()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-switch":
            self.action_switch_selected()
        elif btn_id == "btn-dry-run":
            self.action_dry_run_selected()
        elif btn_id == "btn-stop-all":
            self.action_stop_all()
        elif btn_id == "btn-refresh":
            self.action_refresh_dashboard()
        elif btn_id == "btn-new":
            self.action_new_profile()
        elif btn_id == "btn-edit":
            self.action_edit_selected()
        elif btn_id == "btn-delete":
            self.action_delete_selected()

    def action_switch_selected(self) -> None:
        if not self.selected_profile:
            return
        log = self.query_one("#action-log", RichLog)
        res = self.engine.switch_profile(self.selected_profile)
        log.write(
            f"[bold green]Switched to profile '{self.selected_profile}'[/bold green] "
            f"({len(res.records)} actions)"
        )
        for rec in res.records:
            status_tag = "[green]OK[/green]" if rec.success else "[red]ERR[/red]"
            log.write(f"  {status_tag} [{rec.action}] {rec.identifier}: {rec.command}")
        self._rebuild_profile_list()
        self.refresh_views()

    def action_dry_run_selected(self) -> None:
        if not self.selected_profile:
            return
        log = self.query_one("#action-log", RichLog)
        if isinstance(self.executor, MockCommandExecutor):
            dry_exec: CommandExecutor = replace(self.executor, dry_run=True)
        else:
            dry_exec = RealCommandExecutor(dry_run=True)
        dry_engine = ProfileEngine(self.profiles_file, self.state_manager, dry_exec)
        res = dry_engine.switch_profile(self.selected_profile)
        log.write(
            f"[bold yellow][DRY-RUN] Preview switch to '{self.selected_profile}':[/bold yellow]"
        )
        if not res.records:
            log.write("  No service changes required.")
        for rec in res.records:
            log.write(f"  [yellow]•[/yellow] [{rec.action}] {rec.identifier} -> {rec.command}")

    def action_stop_all(self) -> None:
        log = self.query_one("#action-log", RichLog)
        res = self.engine.stop_all()
        log.write("[bold yellow]Stopped active profile (returned to Idle).[/bold yellow]")
        for rec in res.records:
            log.write(f"  [{rec.action}] {rec.identifier}: {rec.command}")
        self._rebuild_profile_list()
        self.refresh_views()

    def action_refresh_dashboard(self) -> None:
        self.refresh_views()
        log = self.query_one("#action-log", RichLog)
        log.write("[dim]Refreshed live service metrics.[/dim]")

    def action_new_profile(self) -> None:
        self.push_screen(
            ProfileEditorModal(executor=self.executor),
            self._on_profile_saved_new,
        )

    def _on_profile_saved_new(self, result: tuple[str, ProfileConfig] | None) -> None:
        if result is None:
            return
        name, new_prof = result
        self.profiles_file.profiles[name] = new_prof
        save_profiles(self.profiles_file, self.config_path)
        self.selected_profile = name
        self._rebuild_profile_list()
        self.refresh_views()
        log = self.query_one("#action-log", RichLog)
        log.write(f"[bold green]Saved profile '{name}' to {self.config_path}[/bold green]")

    def action_edit_selected(self) -> None:
        if not self.selected_profile or self.selected_profile not in self.profiles_file.profiles:
            return
        orig_name = self.selected_profile
        orig_prof = self.profiles_file.profiles[orig_name]

        def _on_edited(result: tuple[str, ProfileConfig] | None) -> None:
            if result is None:
                return
            new_name, updated_prof = result
            if new_name != orig_name:
                self.profiles_file.profiles.pop(orig_name, None)
            self.profiles_file.profiles[new_name] = updated_prof
            save_profiles(self.profiles_file, self.config_path)
            self.selected_profile = new_name
            self._rebuild_profile_list()
            self.refresh_views()
            log = self.query_one("#action-log", RichLog)
            log.write(f"[bold cyan]Updated profile '{new_name}'.[/bold cyan]")

        self.push_screen(
            ProfileEditorModal(orig_name, orig_prof, executor=self.executor),
            _on_edited,
        )

    def action_delete_selected(self) -> None:
        if not self.selected_profile or self.selected_profile not in self.profiles_file.profiles:
            return
        target = self.selected_profile

        def _on_confirmed(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self.profiles_file.profiles.pop(target, None)
            save_profiles(self.profiles_file, self.config_path)
            self.selected_profile = next(iter(self.profiles_file.profiles.keys()), None)
            self._rebuild_profile_list()
            self.refresh_views()
            log = self.query_one("#action-log", RichLog)
            log.write(f"[bold red]Deleted profile '{target}'.[/bold red]")

        self.push_screen(ConfirmDeleteModal(target), _on_confirmed)
