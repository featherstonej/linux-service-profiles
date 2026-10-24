"""Main Textual TUI application for Linux Service Profiles (srvp)."""

from __future__ import annotations

import os
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

from srvp.config import (
    ProfileConfig,
    ProfileConflictError,
    ProfilesFile,
    load_profiles,
    update_profiles,
)
from srvp.discovery import build_snapshot_profile
from srvp.engine import ProfileEngine, TransitionPlan, TransitionResult
from srvp.executor import CommandExecutor, MockCommandExecutor, RealCommandExecutor
from srvp.providers import ServiceStatus
from srvp.state import StateManager
from srvp.templates import build_starter_profiles
from srvp.tui.editor import ConfirmDeleteModal, ProfileEditorModal


class ProfileListItem(ListItem):
    """ListItem storing its associated profile_name."""

    def __init__(self, profile_name: str, is_active: bool) -> None:
        super().__init__()
        self.profile_name = profile_name
        self.is_active = is_active

    def _format_label(self) -> str:
        badge = " [bold green]● ACTIVE[/bold green]" if self.is_active else ""
        return f"{self.profile_name}{badge}"

    def compose(self) -> ComposeResult:
        yield Label(self._format_label())

    def set_active(self, is_active: bool) -> None:
        if self.is_active != is_active:
            self.is_active = is_active
            for lbl in self.query(Label):
                lbl.update(self._format_label())


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
        min-height: 4;
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
        Binding("c", "clone_selected", "Clone Profile"),
        Binding("m", "snapshot_running", "Snapshot Running"),
        Binding("delete", "delete_selected", "Delete Profile"),
        Binding("r", "refresh_dashboard", "Refresh Status"),
        Binding("p", "toggle_auto_refresh", "Pause Auto-Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        executor: CommandExecutor,
        auto_refresh_interval: float = 4.0,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        is_mock = isinstance(executor, MockCommandExecutor) or os.environ.get("SRVP_MOCK") == "1"
        self.state_manager = StateManager(state_path, mock=is_mock)
        self.executor = executor
        self.auto_refresh_enabled: bool = True
        self.auto_refresh_interval: float = auto_refresh_interval
        self._transition_in_progress: bool = False

        if not self.config_path.exists():
            update_profiles(
                self.config_path,
                lambda _: None,
                initial=build_starter_profiles(),
            )

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
                with Horizontal(classes="action-row"):
                    yield Button("Clone (c)", id="btn-clone")
                    yield Button("Snapshot (m)", id="btn-snapshot")
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
        if self.auto_refresh_interval > 0:
            self.set_interval(self.auto_refresh_interval, self._on_auto_refresh_tick)

    def _on_auto_refresh_tick(self) -> None:
        if (
            self.auto_refresh_enabled
            and not self._transition_in_progress
            and len(self.screen_stack) == 1
        ):
            self.refresh_views()

    def _load_active_profile_safe(self) -> str | None:
        try:
            return self.state_manager.load().active_profile
        except Exception:
            return None

    def _sync_active_profile_indicators(self, active_name: str | None) -> None:
        auto_str = "[green]ON[/green]" if self.auto_refresh_enabled else "[yellow]PAUSED[/yellow]"
        banner = self.query_one("#active-banner", Static)
        active_part = (
            f"[bold green]{active_name}[/bold green]" if active_name else "[dim]None (Idle)[/dim]"
        )
        banner.update(f"Active: {active_part} | Auto: {auto_str}")
        list_view = self.query_one("#profile-list", ListView)
        for item in list_view.query(ProfileListItem):
            item.set_active(item.profile_name == active_name)

    def _update_active_banner(self) -> None:
        self._sync_active_profile_indicators(self._load_active_profile_safe())

    def _rebuild_profile_list(self) -> None:
        active_name = self._load_active_profile_safe()
        self._sync_active_profile_indicators(active_name)

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
        """Schedule a background refresh of the profile summary and live service status table."""
        selected = self.selected_profile
        prof = (
            self.profiles_file.profiles[selected].model_copy(deep=True)
            if (selected and selected in self.profiles_file.profiles)
            else None
        )

        def _worker() -> None:
            active_name = self._load_active_profile_safe()
            try:
                plan = self.engine.compute_transition_plan(selected) if prof is not None else None
            except Exception:
                plan = None
            try:
                statuses = self.engine.get_profile_statuses(selected)
            except Exception:
                statuses = []
            self.call_from_thread(
                self._apply_refreshed_views, selected, prof, plan, statuses, active_name
            )

        self.run_worker(_worker, thread=True, exclusive=True, group="status-refresh")

    def _apply_refreshed_views(
        self,
        selected: str | None,
        prof: ProfileConfig | None,
        plan: TransitionPlan | None,
        statuses: list[ServiceStatus],
        active_name: str | None = None,
    ) -> None:
        """Apply refreshed transition preview and status rows on the UI thread."""
        self._sync_active_profile_indicators(active_name)
        if selected != self.selected_profile:
            return
        details = self.query_one("#profile-details", Static)
        if selected and prof is not None and plan is not None:
            to_start = ", ".join(it.identifier for it in plan.items_to_start) or "none"
            to_stop = ", ".join(it.identifier for it in plan.items_to_stop) or "none"
            details.update(
                f"[bold]{selected}[/bold] — {prof.description}\n"
                f"[green]Will Ensure Active:[/green] {to_start}\n"
                f"[yellow]Will Ensure Stopped:[/yellow] {to_stop}\n"
                f"[cyan]Hooks:[/cyan] {len(prof.hooks.pre_start)} pre-start, "
                f"{len(prof.hooks.post_start)} post-start"
            )
        else:
            details.update("No profile selected.")

        table = self.query_one("#status-table", DataTable)
        table.clear()
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
        elif btn_id == "btn-clone":
            self.action_clone_selected()
        elif btn_id == "btn-snapshot":
            self.action_snapshot_running()
        elif btn_id == "btn-delete":
            self.action_delete_selected()

    def _write_transition_records(self, log: RichLog, res: TransitionResult) -> None:
        if not res.records:
            log.write("  No service changes required.")
            return
        for rec in res.records:
            if res.dry_run or rec.dry_run:
                status_tag = "[yellow]DRY-RUN[/yellow]"
            elif rec.success:
                status_tag = "[green]OK[/green]"
            else:
                status_tag = "[red]ERR[/red]"
            detail = (
                f" — [red]{rec.message}[/red]"
                if (not rec.success and not rec.dry_run and rec.message)
                else ""
            )
            log.write(f"  {status_tag} [{rec.action}] {rec.identifier}: {rec.command}{detail}")

    def action_switch_selected(self) -> None:
        if not self.selected_profile:
            return
        log = self.query_one("#action-log", RichLog)
        if self._transition_in_progress:
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        target = self.selected_profile
        self._transition_in_progress = True

        def _worker() -> None:
            try:
                res = self.engine.switch_profile(target)
            except Exception as exc:
                self.call_from_thread(self._on_transition_exception, f"Switch to '{target}'", exc)
                return
            self.call_from_thread(self._apply_switch_result, target, res)

        self.run_worker(_worker, thread=True, group="transition")

    def _on_transition_exception(self, context: str, exc: Exception) -> None:
        self._transition_in_progress = False
        log = self.query_one("#action-log", RichLog)
        log.write(f"[bold red]{context} failed with error: {exc}[/bold red]")
        self._rebuild_profile_list()
        self.refresh_views()

    def _apply_switch_result(self, target: str, res: TransitionResult) -> None:
        self._transition_in_progress = False
        log = self.query_one("#action-log", RichLog)
        if res.dry_run:
            log.write(
                f"[bold yellow][DRY-RUN] Preview switch to profile '{target}'[/bold yellow] "
                f"({len(res.records)} actions)"
            )
        elif res.success:
            log.write(
                f"[bold green]Switched to profile '{target}'[/bold green] "
                f"({len(res.records)} actions)"
            )
        else:
            log.write(
                f"[bold red]Failed to switch to profile '{target}'[/bold red] "
                f"({len(res.records)} actions)"
            )
        self._write_transition_records(log, res)
        self._rebuild_profile_list()
        self.refresh_views()

    def action_dry_run_selected(self) -> None:
        if not self.selected_profile:
            return
        log = self.query_one("#action-log", RichLog)
        if self._transition_in_progress:
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        target = self.selected_profile
        self._transition_in_progress = True
        if isinstance(self.executor, MockCommandExecutor):
            dry_exec: CommandExecutor = replace(self.executor, dry_run=True)
        else:
            dry_exec = RealCommandExecutor(dry_run=True)
        dry_engine = ProfileEngine(self.profiles_file, self.state_manager, dry_exec)

        def _worker() -> None:
            try:
                res = dry_engine.switch_profile(target)
            except Exception as exc:
                self.call_from_thread(
                    self._on_transition_exception, f"Dry-run preview for '{target}'", exc
                )
                return
            self.call_from_thread(self._apply_dry_run_result, target, res)

        self.run_worker(_worker, thread=True, group="transition")

    def _apply_dry_run_result(self, target: str, res: TransitionResult) -> None:
        self._transition_in_progress = False
        log = self.query_one("#action-log", RichLog)
        log.write(f"[bold yellow][DRY-RUN] Preview switch to '{target}':[/bold yellow]")
        self._write_transition_records(log, res)

    def action_stop_all(self) -> None:
        log = self.query_one("#action-log", RichLog)
        if self._transition_in_progress:
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        self._transition_in_progress = True

        def _worker() -> None:
            try:
                res = self.engine.stop_all()
            except Exception as exc:
                self.call_from_thread(self._on_transition_exception, "Stop All", exc)
                return
            self.call_from_thread(self._apply_stop_result, res)

        self.run_worker(_worker, thread=True, group="transition")

    def _apply_stop_result(self, res: TransitionResult) -> None:
        self._transition_in_progress = False
        log = self.query_one("#action-log", RichLog)
        if res.dry_run:
            log.write(
                f"[bold yellow][DRY-RUN] Preview Stop All (Idle) ({len(res.records)} actions):[/bold yellow]"
            )
        elif res.success:
            log.write("[bold yellow]Stopped active profile (returned to Idle).[/bold yellow]")
        else:
            log.write(
                "[bold red]Failed to stop active profile; some services remain active.[/bold red]"
            )
        self._write_transition_records(log, res)
        self._rebuild_profile_list()
        self.refresh_views()

    def action_refresh_dashboard(self) -> None:
        self.refresh_views()
        log = self.query_one("#action-log", RichLog)
        log.write("[dim]Refreshed live service metrics.[/dim]")

    def action_toggle_auto_refresh(self) -> None:
        self.auto_refresh_enabled = not self.auto_refresh_enabled
        self._update_active_banner()
        log = self.query_one("#action-log", RichLog)
        state_word = "resumed" if self.auto_refresh_enabled else "paused"
        log.write(f"[cyan]Live auto-refresh {state_word}.[/cyan]")

    def action_new_profile(self) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        self.push_screen(
            ProfileEditorModal(
                executor=self.executor,
                existing_names=set(self.profiles_file.profiles.keys()),
                editing_name=None,
            ),
            self._on_profile_saved_new,
        )

    def action_clone_selected(self) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        if not self.selected_profile or self.selected_profile not in self.profiles_file.profiles:
            return
        orig_name = self.selected_profile
        cloned_prof = self.profiles_file.profiles[orig_name].model_copy(deep=True)
        candidate_name = f"{orig_name}-copy"
        self.push_screen(
            ProfileEditorModal(
                candidate_name,
                cloned_prof,
                executor=self.executor,
                existing_names=set(self.profiles_file.profiles.keys()),
                editing_name=None,
            ),
            self._on_profile_saved_new,
        )

    def action_snapshot_running(self) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return

        def _worker() -> None:
            snap_prof = build_snapshot_profile(self.executor)
            self.call_from_thread(self._open_snapshot_editor, snap_prof)

        self.run_worker(_worker, thread=True, exclusive=True, group="snapshot")

    def _open_snapshot_editor(self, snap_prof: ProfileConfig) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        self.push_screen(
            ProfileEditorModal(
                "snapshot-profile",
                snap_prof,
                executor=self.executor,
                existing_names=set(self.profiles_file.profiles.keys()),
                editing_name=None,
            ),
            self._on_profile_saved_new,
        )

    def _on_profile_saved_new(self, result: tuple[str, ProfileConfig] | None) -> None:
        if result is None:
            return
        log = self.query_one("#action-log", RichLog)
        if self._transition_in_progress:
            log.write(
                "[yellow]A transition is in progress; cannot save profile changes right now.[/yellow]"
            )
            return
        name, new_prof = result

        def _add(current: ProfilesFile) -> None:
            if name in current.profiles:
                raise ProfileConflictError(
                    f"Cannot create profile '{name}': a profile with that name already exists."
                )
            current.profiles[name] = new_prof

        try:
            self.profiles_file = update_profiles(self.config_path, _add)
        except ProfileConflictError as exc:
            log.write(f"[bold red]{exc}[/bold red]")
            return
        self.engine.config = self.profiles_file
        self.selected_profile = name
        self._rebuild_profile_list()
        self.refresh_views()
        log.write(f"[bold green]Saved profile '{name}' to {self.config_path}[/bold green]")

    def action_edit_selected(self) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        if not self.selected_profile or self.selected_profile not in self.profiles_file.profiles:
            return
        orig_name = self.selected_profile
        orig_prof = self.profiles_file.profiles[orig_name]
        original_profile = orig_prof.model_copy(deep=True)

        def _on_edited(result: tuple[str, ProfileConfig] | None) -> None:
            if result is None:
                return
            log = self.query_one("#action-log", RichLog)
            if self._transition_in_progress:
                log.write(
                    "[yellow]A transition is in progress; cannot save profile changes right now.[/yellow]"
                )
                return
            new_name, updated_prof = result

            def _update(current: ProfilesFile) -> None:
                if current.profiles.get(orig_name) != original_profile:
                    raise ProfileConflictError(
                        f"Cannot update profile '{orig_name}': it changed since it was loaded."
                    )
                if new_name != orig_name and new_name in current.profiles:
                    raise ProfileConflictError(
                        f"Cannot rename '{orig_name}' to '{new_name}': "
                        f"profile '{new_name}' already exists."
                    )
                if new_name != orig_name:
                    current.profiles.pop(orig_name)
                current.profiles[new_name] = updated_prof

            try:
                self.profiles_file = update_profiles(self.config_path, _update)
            except ProfileConflictError as exc:
                log.write(f"[bold red]{exc}[/bold red]")
                return
            self.engine.config = self.profiles_file
            if new_name != orig_name:
                self.state_manager.rename_active_profile(orig_name, new_name)
            self.selected_profile = new_name
            self._rebuild_profile_list()
            self.refresh_views()
            log.write(f"[bold cyan]Updated profile '{new_name}'.[/bold cyan]")

        self.push_screen(
            ProfileEditorModal(
                orig_name,
                orig_prof,
                executor=self.executor,
                existing_names=set(self.profiles_file.profiles.keys()),
                editing_name=orig_name,
            ),
            _on_edited,
        )

    def action_delete_selected(self) -> None:
        if self._transition_in_progress:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]A transition is already in progress; please wait.[/yellow]")
            return
        if not self.selected_profile or self.selected_profile not in self.profiles_file.profiles:
            return
        target = self.selected_profile
        original_profile = self.profiles_file.profiles[target].model_copy(deep=True)
        try:
            active_now = self.state_manager.load().active_profile
        except Exception as exc:
            log = self.query_one("#action-log", RichLog)
            log.write(f"[bold red]Cannot verify active profile state: {exc}[/bold red]")
            return
        if active_now == target:
            log = self.query_one("#action-log", RichLog)
            log.write(
                f"[bold red]Cannot delete active profile '{target}'. "
                "Stop it first (x) or switch to another profile before deleting.[/bold red]"
            )
            return

        def _on_confirmed(confirmed: bool | None) -> None:
            if not confirmed:
                return
            log = self.query_one("#action-log", RichLog)
            if self._transition_in_progress:
                log.write(
                    "[yellow]A transition is in progress; cannot delete profile right now.[/yellow]"
                )
                return
            try:
                current_active = self.state_manager.load().active_profile
            except Exception as exc:
                log.write(f"[bold red]Cannot verify active profile state: {exc}[/bold red]")
                return
            if current_active == target:
                log.write(
                    f"[bold red]Cannot delete active profile '{target}'. "
                    "Stop it first (x) or switch to another profile before deleting.[/bold red]"
                )
                return

            def _delete(current: ProfilesFile) -> None:
                if current.profiles.get(target) != original_profile:
                    raise ProfileConflictError(
                        f"Cannot delete profile '{target}': it changed since it was loaded."
                    )
                current.profiles.pop(target)

            try:
                self.profiles_file = update_profiles(self.config_path, _delete)
            except ProfileConflictError as exc:
                log.write(f"[bold red]{exc}[/bold red]")
                return
            self.engine.config = self.profiles_file
            self.selected_profile = next(iter(self.profiles_file.profiles.keys()), None)
            self._rebuild_profile_list()
            self.refresh_views()
            log.write(f"[bold red]Deleted profile '{target}'.[/bold red]")

        self.push_screen(ConfirmDeleteModal(target), _on_confirmed)
