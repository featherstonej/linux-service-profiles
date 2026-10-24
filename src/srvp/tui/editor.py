"""Modal screens for creating, editing, and deleting profiles inside the Textual TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from srvp.config import (
    AndroidConfig,
    DockerComposeStack,
    DockerConfig,
    Esp32SerialConfig,
    HooksConfig,
    ProfileConfig,
    SystemdConfig,
)


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


class ProfileEditorModal(ModalScreen[tuple[str, ProfileConfig] | None]):
    """Interactive form modal to create a new profile or edit an existing one."""

    CSS = """
    ProfileEditorModal {
        align: center middle;
    }
    #editor-dialog {
        width: 86;
        height: 88%;
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
    #editor-error {
        color: $error;
        text-style: bold;
        margin-top: 1;
    }
    #editor-buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    #editor-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        profile_name: str = "",
        profile: ProfileConfig | None = None,
    ) -> None:
        super().__init__()
        self.initial_name = profile_name
        self.initial_profile = profile or ProfileConfig()

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
            yield Input(
                value=", ".join(p.systemd.system),
                placeholder="e.g., postgresql.service, redis-server.service",
                id="systemd-system",
            )

            yield Label("Systemd User Units to Start (comma-separated):", classes="field-label")
            yield Input(
                value=", ".join(p.systemd.user),
                placeholder="e.g., syncthing.service",
                id="systemd-user",
            )

            yield Label("Systemd Units to Ensure Stopped (comma-separated):", classes="field-label")
            yield Input(
                value=", ".join(p.systemd.ensure_stopped),
                placeholder="e.g., docker.service, ModemManager.service",
                id="systemd-stopped",
            )

            yield Checkbox(
                "Enable Docker Daemon (docker.service)",
                value=p.docker.enabled,
                id="docker-enabled",
            )

            yield Label("Standalone Docker Containers (comma-separated):", classes="field-label")
            yield Input(
                value=", ".join(p.docker.containers),
                placeholder="e.g., local-postgres, local-redis",
                id="docker-containers",
            )

            yield Label(
                "Docker Compose Stacks (name:/path/to/compose.yml, comma-separated):",
                classes="field-label",
            )
            yield Input(
                value=_format_compose_stacks(p.docker.compose_stacks),
                placeholder="e.g., api:~/proj/docker-compose.yml",
                id="docker-compose",
            )

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
            yield Input(
                value="; ".join(p.hooks.pre_start),
                placeholder="e.g., echo 'Preparing environment'",
                id="hooks-pre-start",
            )

            yield Label("Post-Start Shell Hooks (semicolon-separated):", classes="field-label")
            yield Input(
                value="; ".join(p.hooks.post_start),
                placeholder="e.g., echo 'Profile active'",
                id="hooks-post-start",
            )

            yield Static("", id="editor-error")

            with Horizontal(id="editor-buttons"):
                yield Button("Save Profile", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-save":
            self._validate_and_save()

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
        width: 54;
        height: 11;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        margin-top: 1;
        align: right middle;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, profile_name: str) -> None:
        super().__init__()
        self.profile_name = profile_name

    def compose(self) -> ComposeResult:
        with Grid(id="confirm-dialog"):
            yield Label(
                f"Delete profile '{self.profile_name}'?\nThis will update profiles.yaml immediately."
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", id="btn-confirm-delete", variant="error")
                yield Button("Cancel", id="btn-cancel-delete")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm-delete":
            self.dismiss(True)
        else:
            self.dismiss(False)
