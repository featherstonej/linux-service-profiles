"""Modal screens for creating, editing, and deleting profiles inside the Textual TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Static

from srvp.config import (
    AndroidConfig,
    DockerComposeStack,
    DockerConfig,
    Esp32SerialConfig,
    HooksConfig,
    ProfileConfig,
    SystemdConfig,
    normalize_unit_name,
)
from srvp.discovery import (
    COMMON_HOOK_PRESETS,
    DiscoveredComposeFile,
    DiscoveredService,
    HookPreset,
    discover_compose_files,
    discover_installed_services,
)
from srvp.executor import CommandExecutor, MockCommandExecutor


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _split_semicolon(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(";") if item.strip()]


def _format_compose_stacks(stacks: list[DockerComposeStack]) -> str:
    return ", ".join(f"{s.name}:{s.file}" for s in stacks)


def _parse_compose_stacks(raw: str) -> list[DockerComposeStack]:
    stacks: list[DockerComposeStack] = []
    for part in _split_csv(raw):
        if ":" not in part:
            raise ValueError(
                f"Invalid Compose entry '{part}'. Expected format 'name:/path/to/compose.yml'."
            )
        name, file_path = part.split(":", 1)
        stacks.append(DockerComposeStack(name=name.strip(), file=file_path.strip()))
    return stacks


class ServicePickerModal(ModalScreen[list[str] | None]):
    """Interactive picker for a specific service/container input field."""

    BINDINGS = [
        Binding("space", "toggle_selected", "Toggle Selected", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    ServicePickerModal {
        align: center middle;
    }
    #picker-dialog {
        width: 92%;
        max-width: 110;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        color: $accent;
    }
    #picker-help {
        color: $text-muted;
        margin-bottom: 1;
    }
    #picker-filters {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }
    #picker-search {
        width: 1fr;
        margin-right: 1;
    }
    #picker-hide-core {
        width: auto;
    }
    #picker-table {
        height: 1fr;
        border: round $primary;
        margin-bottom: 1;
    }
    #picker-buttons {
        width: 100%;
        height: 3;
        align: right middle;
    }
    #picker-buttons Button {
        width: 1fr;
        min-width: 8;
        padding: 0;
        margin-right: 1;
    }
    """

    def __init__(
        self,
        executor: CommandExecutor,
        category: str,
        current_items: list[str],
        title: str,
    ) -> None:
        super().__init__()
        self.executor = executor
        self.category = category
        self.title_text = title
        all_discovered = discover_installed_services(executor)
        self.discovered: list[DiscoveredService] = [
            d for d in all_discovered if d.category == category
        ]
        self.search_query: str = ""
        # Show all installed units by default (Option B), with optional checkbox to hide core OS units
        self.hide_core_os: bool = False
        self._visible_items: list[DiscoveredService] = []

        self.selected_names: set[str] = set()
        for item in current_items:
            cleaned = item.strip()
            if not cleaned:
                continue
            norm = (
                normalize_unit_name(cleaned)
                if category in ("systemd_system", "systemd_user")
                else cleaned
            )
            self.selected_names.add(norm)

        known_names = {d.name for d in self.discovered}
        for name in sorted(self.selected_names):
            if name not in known_names:
                self.discovered.insert(
                    0,
                    DiscoveredService(
                        category=category,
                        name=name,
                        machine_state="configured",
                        is_core_os=False,
                    ),
                )

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Label(self.title_text, id="picker-title")
            yield Static(
                "Select a row and press [bold]Space/Enter[/bold] to toggle selection, "
                "then click [bold]Apply[/bold] to populate the input box.",
                id="picker-help",
            )
            with Horizontal(id="picker-filters"):
                yield Input(
                    placeholder="Type to search/filter installed services...",
                    id="picker-search",
                )
                if self.category == "systemd_system":
                    yield Checkbox(
                        "Hide core OS units",
                        value=False,
                        id="picker-hide-core",
                    )
            yield DataTable(id="picker-table")
            with Horizontal(id="picker-buttons"):
                yield Button("Toggle (Space)", id="btn-picker-toggle", variant="success")
                yield Button("Clear All", id="btn-picker-clear", variant="warning")
                yield Button("Apply", id="btn-picker-apply", variant="primary")
                yield Button("Cancel", id="btn-picker-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Selected",
            "Service / Container",
            "Current Machine State",
        )
        self._refresh_table()
        table.focus()

    def _refresh_table(self, preserve_cursor: int | None = None) -> None:
        table = self.query_one("#picker-table", DataTable)
        prev_row = preserve_cursor if preserve_cursor is not None else table.cursor_row
        table.clear()
        self._visible_items = []

        q = self.search_query.lower().strip()
        for item in self.discovered:
            is_sel = item.name in self.selected_names
            if item.is_core_os and self.hide_core_os and not is_sel:
                continue
            if q and q not in item.name.lower():
                continue
            self._visible_items.append(item)
            badge = "[bold green]✓ Selected[/bold green]" if is_sel else "[dim]· Unselected[/dim]"
            table.add_row(badge, item.name, item.machine_state)

        if self._visible_items and 0 <= prev_row < len(self._visible_items):
            table.move_cursor(row=prev_row)

    def action_toggle_selected(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._visible_items):
            item = self._visible_items[row_idx]
            if item.name in self.selected_names:
                self.selected_names.discard(item.name)
            else:
                self.selected_names.add(item.name)
            self._refresh_table(preserve_cursor=row_idx)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_toggle_selected()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-search":
            self.search_query = event.value
            self._refresh_table(preserve_cursor=0)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "picker-hide-core":
            self.hide_core_os = event.value
            self._refresh_table(preserve_cursor=0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-picker-toggle":
            self.action_toggle_selected()
        elif bid == "btn-picker-clear":
            self.selected_names.clear()
            self._refresh_table(preserve_cursor=0)
        elif bid == "btn-picker-cancel":
            self.dismiss(None)
        elif bid == "btn-picker-apply":
            self._apply_and_close()

    def _apply_and_close(self) -> None:
        self.dismiss(sorted(self.selected_names))


class ComposePickerModal(ModalScreen[list[DockerComposeStack] | None]):
    """Interactive modal for selecting discovered Docker Compose files or adding a stack."""

    BINDINGS = [
        Binding("space", "toggle_selected", "Toggle Selected", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    ComposePickerModal {
        align: center middle;
    }
    #compose-picker-dialog {
        width: 90%;
        max-width: 105;
        height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #compose-picker-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #compose-table {
        height: 1fr;
        border: round $primary;
        margin-bottom: 1;
    }
    #compose-custom-row {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }
    #compose-custom-name {
        width: 26;
        margin-right: 1;
    }
    #compose-custom-path {
        width: 1fr;
        margin-right: 1;
    }
    #btn-compose-add {
        min-width: 10;
        padding: 0 1;
    }
    #compose-buttons {
        width: 100%;
        height: 3;
        align: right middle;
    }
    #compose-buttons Button {
        width: 1fr;
        min-width: 8;
        padding: 0;
        margin-right: 1;
    }
    """

    def __init__(
        self,
        executor: CommandExecutor,
        existing_stacks: list[DockerComposeStack],
    ) -> None:
        super().__init__()
        self.discovered: list[DiscoveredComposeFile] = discover_compose_files(executor)
        self.selected_files: dict[str, str] = {s.file: s.name for s in existing_stacks}

        known_files = {d.file_path for d in self.discovered}
        for s in existing_stacks:
            if s.file not in known_files:
                self.discovered.insert(
                    0,
                    DiscoveredComposeFile(stack_name=s.name, file_path=s.file),
                )

    def compose(self) -> ComposeResult:
        with Vertical(id="compose-picker-dialog"):
            yield Label(
                "Select Docker Compose Stacks (Discovered in Project Folders)",
                id="compose-picker-title",
            )
            yield Static(
                "Press [bold]Space/Enter[/bold] on a row to toggle selection, or add a custom Compose file below:"
            )
            yield DataTable(id="compose-table")
            with Horizontal(id="compose-custom-row"):
                yield Input(placeholder="Stack name (e.g. api)", id="compose-custom-name")
                yield Input(
                    placeholder="Path to docker-compose.yml",
                    id="compose-custom-path",
                )
                yield Button("Add", id="btn-compose-add", variant="success")
            with Horizontal(id="compose-buttons"):
                yield Button("Toggle (Space)", id="btn-compose-toggle")
                yield Button("Apply", id="btn-compose-apply", variant="primary")
                yield Button("Cancel", id="btn-compose-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#compose-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Selected", "Stack Name", "Compose File Path")
        self._refresh_table()
        table.focus()

    def _refresh_table(self, preserve_cursor: int | None = None) -> None:
        table = self.query_one("#compose-table", DataTable)
        prev_row = preserve_cursor if preserve_cursor is not None else table.cursor_row
        table.clear()
        for item in self.discovered:
            is_sel = item.file_path in self.selected_files
            badge = "[bold green]✓ Selected[/bold green]" if is_sel else "[dim]· Unselected[/dim]"
            name = self.selected_files.get(item.file_path, item.stack_name)
            table.add_row(badge, name, item.file_path)
        if self.discovered and 0 <= prev_row < len(self.discovered):
            table.move_cursor(row=prev_row)

    def action_toggle_selected(self) -> None:
        table = self.query_one("#compose-table", DataTable)
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self.discovered):
            item = self.discovered[row_idx]
            if item.file_path in self.selected_files:
                self.selected_files.pop(item.file_path, None)
            else:
                self.selected_files[item.file_path] = item.stack_name
            self._refresh_table(preserve_cursor=row_idx)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_toggle_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-compose-toggle":
            self.action_toggle_selected()
        elif bid == "btn-compose-add":
            name_in = self.query_one("#compose-custom-name", Input)
            path_in = self.query_one("#compose-custom-path", Input)
            raw_path = path_in.value.strip()
            raw_name = name_in.value.strip() or "stack"
            if raw_path:
                self.discovered.append(
                    DiscoveredComposeFile(stack_name=raw_name, file_path=raw_path)
                )
                self.selected_files[raw_path] = raw_name
                name_in.value = ""
                path_in.value = ""
                self._refresh_table(preserve_cursor=len(self.discovered) - 1)
        elif bid == "btn-compose-cancel":
            self.dismiss(None)
        elif bid == "btn-compose-apply":
            result = [
                DockerComposeStack(name=name, file=fpath)
                for fpath, name in self.selected_files.items()
            ]
            self.dismiss(result)


class HookPickerModal(ModalScreen[list[str] | None]):
    """Interactive modal allowing users to pick common shell hook templates or add commands."""

    BINDINGS = [
        Binding("space", "toggle_selected", "Toggle Hook", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    HookPickerModal {
        align: center middle;
    }
    #hook-picker-dialog {
        width: 90%;
        max-width: 105;
        height: 82%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #hook-picker-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #hook-table {
        height: 1fr;
        border: round $primary;
        margin-bottom: 1;
    }
    #hook-custom-row {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }
    #hook-custom-cmd {
        width: 1fr;
        margin-right: 1;
    }
    #btn-hook-add {
        min-width: 10;
        padding: 0 1;
    }
    #hook-buttons {
        width: 100%;
        height: 3;
        align: right middle;
    }
    #hook-buttons Button {
        width: 1fr;
        min-width: 8;
        padding: 0;
        margin-right: 1;
    }
    """

    def __init__(self, stage: str, current_commands: list[str]) -> None:
        super().__init__()
        self.stage = stage
        self.presets: list[HookPreset] = [p for p in COMMON_HOOK_PRESETS if p.stage == stage]
        self.selected_commands: list[str] = list(current_commands)

        known_cmds = {p.command for p in self.presets}
        for cmd in current_commands:
            if cmd not in known_cmds:
                self.presets.insert(
                    0,
                    HookPreset(stage=stage, label="Custom command", command=cmd),
                )

    def compose(self) -> ComposeResult:
        stage_title = "Pre-Start" if self.stage == "pre_start" else "Post-Start"
        with Vertical(id="hook-picker-dialog"):
            yield Label(f"Select {stage_title} Shell Hooks", id="hook-picker-title")
            yield Static(
                "Press [bold]Space/Enter[/bold] to toggle a preset hook, or add a custom command below:"
            )
            yield DataTable(id="hook-table")
            with Horizontal(id="hook-custom-row"):
                yield Input(
                    placeholder="Custom shell command (e.g. echo 'Ready')",
                    id="hook-custom-cmd",
                )
                yield Button("Add", id="btn-hook-add", variant="success")
            with Horizontal(id="hook-buttons"):
                yield Button("Toggle (Space)", id="btn-hook-toggle")
                yield Button("Apply", id="btn-hook-apply", variant="primary")
                yield Button("Cancel", id="btn-hook-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#hook-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Selected", "Description", "Shell Command")
        self._refresh_table()
        table.focus()

    def _refresh_table(self, preserve_cursor: int | None = None) -> None:
        table = self.query_one("#hook-table", DataTable)
        prev_row = preserve_cursor if preserve_cursor is not None else table.cursor_row
        table.clear()
        for preset in self.presets:
            is_sel = preset.command in self.selected_commands
            badge = "[bold green]✓ Selected[/bold green]" if is_sel else "[dim]· Unselected[/dim]"
            table.add_row(badge, preset.label, preset.command)
        if self.presets and 0 <= prev_row < len(self.presets):
            table.move_cursor(row=prev_row)

    def action_toggle_selected(self) -> None:
        table = self.query_one("#hook-table", DataTable)
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self.presets):
            cmd = self.presets[row_idx].command
            if cmd in self.selected_commands:
                self.selected_commands.remove(cmd)
            else:
                self.selected_commands.append(cmd)
            self._refresh_table(preserve_cursor=row_idx)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_toggle_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-hook-toggle":
            self.action_toggle_selected()
        elif bid == "btn-hook-add":
            cmd_in = self.query_one("#hook-custom-cmd", Input)
            raw_cmd = cmd_in.value.strip()
            if raw_cmd:
                self.presets.append(
                    HookPreset(stage=self.stage, label="Custom command", command=raw_cmd)
                )
                if raw_cmd not in self.selected_commands:
                    self.selected_commands.append(raw_cmd)
                cmd_in.value = ""
                self._refresh_table(preserve_cursor=len(self.presets) - 1)
        elif bid == "btn-hook-cancel":
            self.dismiss(None)
        elif bid == "btn-hook-apply":
            self.dismiss(list(self.selected_commands))


class ProfileEditorModal(ModalScreen[tuple[str, ProfileConfig] | None]):
    """Interactive form modal to create a new profile or edit an existing one."""

    CSS = """
    ProfileEditorModal {
        align: center middle;
    }
    #editor-dialog {
        width: 90;
        height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #editor-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .field-label {
        margin-top: 1;
        color: $text-muted;
    }
    .input-with-picker {
        width: 100%;
        height: 3;
    }
    .input-with-picker Input {
        width: 1fr;
        margin-right: 1;
    }
    .input-with-picker Button {
        min-width: 12;
        padding: 0 1;
    }
    #editor-error {
        color: $error;
        text-style: bold;
        margin-top: 1;
    }
    #editor-buttons {
        width: 100%;
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    #editor-buttons Button {
        margin-left: 1;
        min-width: 14;
    }
    """

    def __init__(
        self,
        profile_name: str = "",
        profile: ProfileConfig | None = None,
        executor: CommandExecutor | None = None,
    ) -> None:
        super().__init__()
        self.initial_name = profile_name
        self.initial_profile = profile or ProfileConfig()
        self.executor: CommandExecutor = executor or MockCommandExecutor()

    def compose(self) -> ComposeResult:
        p = self.initial_profile
        title = (
            f"Edit Profile: {self.initial_name}"
            if self.initial_name
            else "Create New Service Profile"
        )
        with VerticalScroll(id="editor-dialog"):
            yield Label(title, id="editor-title")

            yield Label("Profile Name (no spaces):", classes="field-label")
            yield Input(
                value=self.initial_name,
                placeholder="e.g., web-db or rust-embedded",
                id="profile-name",
            )

            yield Label("Description:", classes="field-label")
            yield Input(
                value=p.description,
                placeholder="Short description of what this profile is for",
                id="profile-desc",
            )

            yield Label("Systemd System Units to Start (comma-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value=", ".join(p.systemd.system),
                    placeholder="e.g., postgresql.service, redis-server.service",
                    id="systemd-system",
                )
                yield Button("Select...", id="btn-pick-sys-start", variant="primary")

            yield Label("Systemd User Units to Start (comma-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value=", ".join(p.systemd.user),
                    placeholder="e.g., syncthing.service",
                    id="systemd-user",
                )
                yield Button("Select...", id="btn-pick-usr-start", variant="primary")

            yield Label("Systemd Units to Ensure Stopped (comma-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value=", ".join(p.systemd.ensure_stopped),
                    placeholder="e.g., docker.service, ModemManager.service",
                    id="systemd-stopped",
                )
                yield Button("Select...", id="btn-pick-sys-stop", variant="primary")

            yield Checkbox(
                "Enable Docker Daemon (docker.service)",
                value=p.docker.enabled,
                id="docker-enabled",
            )

            yield Label("Standalone Docker Containers (comma-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value=", ".join(p.docker.containers),
                    placeholder="e.g., local-postgres, local-redis",
                    id="docker-containers",
                )
                yield Button("Select...", id="btn-pick-docker", variant="primary")

            yield Label(
                "Docker Compose Stacks (name:/path/to/compose.yml, comma-separated):",
                classes="field-label",
            )
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value=_format_compose_stacks(p.docker.compose_stacks),
                    placeholder="e.g., api:~/proj/docker-compose.yml",
                    id="docker-compose",
                )
                yield Button("Select...", id="btn-pick-compose", variant="primary")

            yield Checkbox(
                "Start Android ADB Server (adb start-server)",
                value=p.android.adb_server,
                id="adb-enabled",
            )

            yield Checkbox(
                "Enable ESP32 / Serial Checks & Conflict Protection (stops brltty/ModemManager)",
                value=p.esp32_serial.enabled,
                id="esp32-enabled",
            )

            yield Label("Pre-Start Shell Hooks (semicolon-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value="; ".join(p.hooks.pre_start),
                    placeholder="e.g., echo 'Preparing environment'",
                    id="hooks-pre-start",
                )
                yield Button("Select...", id="btn-pick-pre-hooks", variant="primary")

            yield Label("Post-Start Shell Hooks (semicolon-separated):", classes="field-label")
            with Horizontal(classes="input-with-picker"):
                yield Input(
                    value="; ".join(p.hooks.post_start),
                    placeholder="e.g., echo 'Profile active'",
                    id="hooks-post-start",
                )
                yield Button("Select...", id="btn-pick-post-hooks", variant="primary")

            yield Static("", id="editor-error")

            with Horizontal(id="editor-buttons"):
                yield Button("Save Profile", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-cancel":
            self.dismiss(None)
        elif bid == "btn-save":
            self._validate_and_save()
        elif bid == "btn-pick-sys-start":
            self._open_service_picker(
                input_id="#systemd-system",
                category="systemd_system",
                title="Select Systemd System Units to Start (Active)",
            )
        elif bid == "btn-pick-usr-start":
            self._open_service_picker(
                input_id="#systemd-user",
                category="systemd_user",
                title="Select Systemd User Units to Start (systemctl --user)",
            )
        elif bid == "btn-pick-sys-stop":
            self._open_service_picker(
                input_id="#systemd-stopped",
                category="systemd_system",
                title="Select Systemd Units to Ensure Stopped (Inactive)",
            )
        elif bid == "btn-pick-docker":
            self._open_service_picker(
                input_id="#docker-containers",
                category="docker_container",
                title="Select Standalone Docker Containers to Start",
            )
        elif bid == "btn-pick-compose":
            self._open_compose_picker()
        elif bid == "btn-pick-pre-hooks":
            self._open_hook_picker("pre_start")
        elif bid == "btn-pick-post-hooks":
            self._open_hook_picker("post_start")

    def _open_service_picker(
        self,
        input_id: str,
        category: str,
        title: str,
    ) -> None:
        current_items = _split_csv(self.query_one(input_id, Input).value)

        def _on_closed(selected: list[str] | None) -> None:
            if selected is None:
                return
            self.query_one(input_id, Input).value = ", ".join(selected)
            if input_id == "#docker-containers" and selected:
                self.query_one("#docker-enabled", Checkbox).value = True

        picker = ServicePickerModal(
            executor=self.executor,
            category=category,
            current_items=current_items,
            title=title,
        )
        self.app.push_screen(picker, _on_closed)

    def _open_compose_picker(self) -> None:
        compose_raw = self.query_one("#docker-compose", Input).value
        try:
            existing = _parse_compose_stacks(compose_raw)
        except ValueError:
            existing = []
        self.app.push_screen(
            ComposePickerModal(self.executor, existing),
            self._on_compose_picker_closed,
        )

    def _on_compose_picker_closed(self, result: list[DockerComposeStack] | None) -> None:
        if result is None:
            return
        self.query_one("#docker-compose", Input).value = _format_compose_stacks(result)
        if result:
            self.query_one("#docker-enabled", Checkbox).value = True

    def _open_hook_picker(self, stage: str) -> None:
        input_id = "#hooks-pre-start" if stage == "pre_start" else "#hooks-post-start"
        current_cmds = _split_semicolon(self.query_one(input_id, Input).value)

        def _on_closed(result: list[str] | None) -> None:
            if result is None:
                return
            self.query_one(input_id, Input).value = "; ".join(result)

        self.app.push_screen(HookPickerModal(stage, current_cmds), _on_closed)

    def _validate_and_save(self) -> None:
        error_widget = self.query_one("#editor-error", Static)
        name = self.query_one("#profile-name", Input).value.strip()
        if not name or " " in name:
            error_widget.update("Error: Profile name must be non-empty and cannot contain spaces.")
            return

        desc = self.query_one("#profile-desc", Input).value.strip()
        sys_units = _split_csv(self.query_one("#systemd-system", Input).value)
        usr_units = _split_csv(self.query_one("#systemd-user", Input).value)
        stop_units = _split_csv(self.query_one("#systemd-stopped", Input).value)
        docker_enabled = self.query_one("#docker-enabled", Checkbox).value
        docker_containers = _split_csv(self.query_one("#docker-containers", Input).value)
        compose_raw = self.query_one("#docker-compose", Input).value
        adb_enabled = self.query_one("#adb-enabled", Checkbox).value
        esp32_enabled = self.query_one("#esp32-enabled", Checkbox).value
        pre_start = _split_semicolon(self.query_one("#hooks-pre-start", Input).value)
        post_start = _split_semicolon(self.query_one("#hooks-post-start", Input).value)

        try:
            compose_stacks = _parse_compose_stacks(compose_raw)
            new_profile = ProfileConfig(
                description=desc,
                systemd=SystemdConfig(
                    system=sys_units,
                    user=usr_units,
                    ensure_stopped=stop_units,
                ),
                docker=DockerConfig(
                    enabled=docker_enabled,
                    containers=docker_containers,
                    compose_stacks=compose_stacks,
                ),
                android=AndroidConfig(adb_server=adb_enabled),
                esp32_serial=Esp32SerialConfig(
                    enabled=esp32_enabled,
                    check_dialout_group=True,
                    stop_conflicting_services=True,
                ),
                hooks=HooksConfig(
                    pre_start=pre_start,
                    post_start=post_start,
                    pre_stop=self.initial_profile.hooks.pre_stop,
                    post_stop=self.initial_profile.hooks.post_stop,
                ),
            )
        except Exception as exc:
            error_widget.update(f"Validation error: {exc}")
            return

        self.dismiss((name, new_profile))


class ConfirmDeleteModal(ModalScreen[bool]):
    """Confirmation dialog before deleting a profile."""

    CSS = """
    ConfirmDeleteModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 56;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        width: 100%;
        height: 3;
        margin-top: 1;
        align: right middle;
    }
    #confirm-buttons Button {
        margin-left: 1;
        min-width: 12;
    }
    """

    def __init__(self, profile_name: str) -> None:
        super().__init__()
        self.profile_name = profile_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(
                f"Delete profile '{self.profile_name}'?\n"
                "This will update profiles.yaml immediately."
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", id="btn-confirm-delete", variant="error")
                yield Button("Cancel", id="btn-cancel-delete")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm-delete":
            self.dismiss(True)
        else:
            self.dismiss(False)
