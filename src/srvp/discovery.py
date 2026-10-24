"""Discovery of installed systemd services (system & user) and Docker containers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from srvp.config import (
    AndroidConfig,
    DockerComposeStack,
    DockerConfig,
    LibvirtConfig,
    PodmanConfig,
    ProfileConfig,
    SystemdConfig,
)
from srvp.executor import CommandExecutor, MockCommandExecutor

# Low-level OS plumbing, network, display-manager, and hardware-daemon prefixes
# hidden by default in the curated view and excluded from automatic snapshots.
CORE_OS_PREFIXES = (
    "systemd-",
    "dbus",
    "udev",
    "getty@",
    "serial-getty@",
    "console-getty",
    "container-getty@",
    "kmod",
    "plymouth",
    "initrd",
    "dracut",
    "grub",
    "fsck",
    "fstrim",
    "lvm2",
    "dm-event",
    "multipath",
    "udisks2",
    "upower",
    "polkit",
    "rtkit-daemon",
    "accounts-daemon",
    "user@",
    "user-runtime-dir@",
    "apparmor",
    "auditd",
    "selinux",
    "ldconfig",
    "alsa-",
    "colord",
    "fwupd",
    "packagekit",
    "switcheroo-control",
    "power-profiles-daemon",
    "thermald",
    "wpa_supplicant",
    "networkmanager",
    "networkd-dispatcher",
    "iwd",
    "connman",
    "modemmanager",
    "brltty",
    "bluetooth",
    "cups",
    "avahi-daemon",
    "gdm",
    "sddm",
    "lightdm",
    "greetd",
    "lxdm",
    "xdm",
    "ly.",
    "display-manager",
    "seatd",
    "logind",
    "sshd",
    "ssh.",
    "ssh@",
    "sysstat",
    "rsyslog",
    "cron",
    "anacron",
    "atd",
    "emergency",
    "rescue",
    "quotaon",
    "rc-local",
)


def is_core_os_unit(unit_name: str) -> bool:
    """Return True if `unit_name` is a low-level OS plumbing unit or bare template unit."""
    if unit_name.endswith("@.service"):
        return True
    lowered = unit_name.lower()
    return any(lowered.startswith(prefix.lower()) for prefix in CORE_OS_PREFIXES)


@dataclass(frozen=True)
class DiscoveredService:
    """Represents an installed service or container discovered on the host machine."""

    category: str  # "systemd_system", "systemd_user", "docker_container"
    name: str
    machine_state: str
    is_core_os: bool = False
    compose_project: str | None = None
    compose_file: str | None = None
    compose_working_dir: str | None = None
    compose_service: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.category, self.name)


def _parse_systemd_units(
    unit_files_out: str,
    live_units_out: str,
    category: str,
) -> list[DiscoveredService]:
    """Combine `systemctl list-unit-files` and `systemctl list-units` output."""
    live_state: dict[str, str] = {}
    for line in live_units_out.splitlines():
        parts = line.split()
        # Strip leading status markers such as '●', '×', '○', '*' when --plain is not used
        while parts and not parts[0].endswith(".service") and len(parts[0]) <= 2:
            parts = parts[1:]
        # Standard columns: UNIT LOAD ACTIVE SUB DESCRIPTION...
        if len(parts) >= 4 and parts[0].endswith(".service"):
            unit = parts[0]
            active_col = parts[2]
            sub_col = parts[3]
            live_state[unit] = f"{active_col} ({sub_col})"

    discovered: dict[str, DiscoveredService] = {}
    for line in unit_files_out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(".service"):
            unit = parts[0]
            file_state = parts[1]
            # Skip static/masked internal units unless they are currently running
            if file_state in ("masked", "masked-runtime") and unit not in live_state:
                continue
            state_str = live_state.get(unit, file_state)
            core = (category == "systemd_system") and is_core_os_unit(unit)
            discovered[unit] = DiscoveredService(
                category=category,
                name=unit,
                machine_state=state_str,
                is_core_os=core,
            )

    # Include any live units not listed in unit-files
    for unit, state_str in live_state.items():
        if unit not in discovered:
            core = (category == "systemd_system") and is_core_os_unit(unit)
            discovered[unit] = DiscoveredService(
                category=category,
                name=unit,
                machine_state=state_str,
                is_core_os=core,
            )

    return sorted(discovered.values(), key=lambda d: (d.is_core_os, d.name.lower()))


def discover_installed_services(executor: CommandExecutor) -> list[DiscoveredService]:
    """Query the host (or MockCommandExecutor) for system units, user units, and Docker containers."""
    results: list[DiscoveredService] = []

    # 1. System-level systemd services
    sys_files = executor.run(
        [
            "systemctl",
            "list-unit-files",
            "--type=service",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        sudo=False,
        mutating=False,
    )
    sys_live = executor.run(
        [
            "systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        sudo=False,
        mutating=False,
    )
    if sys_files.returncode == 0 or sys_live.returncode == 0:
        results.extend(_parse_systemd_units(sys_files.stdout, sys_live.stdout, "systemd_system"))

    # 2. User-level systemd services
    usr_files = executor.run(
        [
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        sudo=False,
        mutating=False,
    )
    usr_live = executor.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        sudo=False,
        mutating=False,
    )
    if usr_files.returncode == 0 or usr_live.returncode == 0:
        results.extend(_parse_systemd_units(usr_files.stdout, usr_live.stdout, "systemd_user"))

    # 3. Existing Docker containers
    if _docker_discovery_is_safe(executor):
        docker_ps = executor.run(
            [
                "docker",
                "ps",
                "-a",
                "--format",
                '{{.Names}}|{{.State}}|{{.Label "com.docker.compose.project"}}|'
                '{{.Label "com.docker.compose.project.config_files"}}|'
                '{{.Label "com.docker.compose.project.working_dir"}}|'
                '{{.Label "com.docker.compose.service"}}',
            ],
            sudo=False,
            mutating=False,
        )
        if docker_ps.returncode == 0 and docker_ps.stdout.strip():
            for line in docker_ps.stdout.splitlines():
                parts = line.strip().split("|", 5)
                if parts and parts[0]:
                    values = parts + [""] * (6 - len(parts))
                    compose_files = values[3].split(",")
                    results.append(
                        DiscoveredService(
                            category="docker_container",
                            name=values[0],
                            machine_state=values[1],
                            compose_project=values[2] or None,
                            compose_file=compose_files[0] or None,
                            compose_working_dir=values[4] or None,
                            compose_service=values[5] or None,
                        )
                    )

    # 4. Existing Podman containers
    podman_ps = executor.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}|{{.State}}"],
        sudo=False,
        mutating=False,
    )
    if podman_ps.returncode == 0 and podman_ps.stdout.strip():
        for line in podman_ps.stdout.splitlines():
            if "|" in line:
                name, state = line.strip().split("|", 1)
                if name:
                    results.append(
                        DiscoveredService(
                            category="podman_container",
                            name=name,
                            machine_state=state,
                            is_core_os=False,
                        )
                    )

    # 5. Existing Libvirt / KVM virtual machines
    virsh_list = executor.run(
        ["virsh", "list", "--all", "--name"],
        sudo=False,
        mutating=False,
    )
    if virsh_list.returncode == 0 and virsh_list.stdout.strip():
        for line in virsh_list.stdout.splitlines():
            vm_name = line.strip()
            if vm_name:
                st_res = executor.run(
                    ["virsh", "domstate", vm_name],
                    sudo=False,
                    mutating=False,
                )
                vm_state = st_res.stdout.strip() if st_res.returncode == 0 else "shut off"
                results.append(
                    DiscoveredService(
                        category="libvirt_vm",
                        name=vm_name,
                        machine_state=vm_state,
                        is_core_os=False,
                    )
                )

    return results


def _docker_discovery_is_safe(executor: CommandExecutor) -> bool:
    """Avoid connecting to an inactive local Docker socket that may be socket-activated."""
    if isinstance(executor, MockCommandExecutor):
        return True

    context = os.environ.get("DOCKER_CONTEXT")
    host = os.environ.get("DOCKER_HOST")
    if context:
        endpoint = executor.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", context],
            sudo=False,
            mutating=False,
        )
        if endpoint.returncode != 0:
            return False
        host = endpoint.stdout.strip()
    elif not host:
        current_context = executor.run(
            ["docker", "context", "show"],
            sudo=False,
            mutating=False,
        )
        context = current_context.stdout.strip() if current_context.returncode == 0 else "default"
        if context and context != "default":
            endpoint = executor.run(
                [
                    "docker",
                    "context",
                    "inspect",
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                    context,
                ],
                sudo=False,
                mutating=False,
            )
            if endpoint.returncode != 0:
                return False
            host = endpoint.stdout.strip()

    if host and not host.startswith("unix://"):
        return host.startswith(("tcp://", "ssh://"))

    rootless_host = os.environ.get("XDG_RUNTIME_DIR")
    is_rootless = bool(
        host and rootless_host and host.removeprefix("unix://").startswith(f"{rootless_host}/")
    )
    status_cmd = (
        ["systemctl", "--user", "is-active", "--quiet", "docker.service"]
        if is_rootless
        else ["systemctl", "is-active", "--quiet", "docker.service"]
    )
    status = executor.run(status_cmd, sudo=False, mutating=False)
    return status.returncode == 0


@dataclass(frozen=True)
class DiscoveredComposeFile:
    """Represents a Docker Compose file discovered on the user's machine."""

    stack_name: str
    file_path: str


COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


def discover_compose_files(executor: CommandExecutor) -> list[DiscoveredComposeFile]:
    """Scan common project directories for Docker Compose files (or return mock stacks in mock mode)."""
    if isinstance(executor, MockCommandExecutor):
        return [
            DiscoveredComposeFile(
                stack_name="webapp-stack",
                file_path="~/projects/webapp/docker-compose.yml",
            ),
            DiscoveredComposeFile(
                stack_name="microservices-api",
                file_path="~/projects/microservices/compose.yaml",
            ),
            DiscoveredComposeFile(
                stack_name="monitoring-stack",
                file_path="~/dev/monitoring/docker-compose.yml",
            ),
        ]

    home = Path.home()
    candidate_roots = [
        Path.cwd(),
        home / "projects",
        home / "Projects",
        home / "AntigravityProjects",
        home / "dev",
        home / "code",
        home / "workspace",
        home / "src",
    ]

    found_paths: set[Path] = set()
    discovered: list[DiscoveredComposeFile] = []

    for root in candidate_roots:
        if not root.exists() or not root.is_dir():
            continue
        # Check root and up to 2 levels of subdirectories without traversing hidden/venv dirs
        dirs_to_check: list[Path] = [root]
        try:
            for child in sorted(root.iterdir()):
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and child.name != "node_modules"
                ):
                    dirs_to_check.append(child)
                    try:
                        for grandchild in sorted(child.iterdir()):
                            if (
                                grandchild.is_dir()
                                and not grandchild.name.startswith(".")
                                and grandchild.name != "node_modules"
                            ):
                                dirs_to_check.append(grandchild)
                    except OSError:
                        continue
        except OSError:
            continue

        for d in dirs_to_check:
            for fname in COMPOSE_FILENAMES:
                candidate = d / fname
                try:
                    if candidate.is_file():
                        resolved = candidate.resolve()
                        if resolved not in found_paths:
                            found_paths.add(resolved)
                            stack_name = d.name.lower().replace(" ", "-") or "stack"
                            try:
                                rel_home = f"~/{resolved.relative_to(home)}"
                            except ValueError:
                                rel_home = str(resolved)
                            discovered.append(
                                DiscoveredComposeFile(
                                    stack_name=stack_name,
                                    file_path=rel_home,
                                )
                            )
                except OSError:
                    continue

    return discovered


@dataclass(frozen=True)
class HookPreset:
    """Curated shell hook preset for users who prefer picking from common actions."""

    stage: str  # "pre_start" or "post_start"
    label: str
    command: str


COMMON_HOOK_PRESETS: list[HookPreset] = [
    HookPreset(
        stage="pre_start",
        label="Set CPU governor / power profile to Performance",
        command="powerprofilesctl set performance 2>/dev/null || true",
    ),
    HookPreset(
        stage="pre_start",
        label="Set CPU governor / power profile to Power Saver (Idle)",
        command="powerprofilesctl set power-saver 2>/dev/null || true",
    ),
    HookPreset(
        stage="pre_start",
        label="Prune stopped Docker networks before starting stack",
        command="docker network prune -f >/dev/null 2>&1 || true",
    ),
    HookPreset(
        stage="pre_start",
        label="Verify serial device permissions (/dev/ttyUSB0)",
        command="ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true",
    ),
    HookPreset(
        stage="post_start",
        label="Send desktop notification when profile is active",
        command="notify-send 'srvp' 'Service profile activated successfully' 2>/dev/null || true",
    ),
    HookPreset(
        stage="post_start",
        label="Log active listening TCP ports (PostgreSQL/Redis/Web)",
        command="ss -tlnp 2>/dev/null | head -n 15 || true",
    ),
    HookPreset(
        stage="post_start",
        label="List connected Android ADB devices",
        command="adb devices 2>/dev/null || true",
    ),
    HookPreset(
        stage="post_start",
        label="Print confirmation banner",
        command="echo 'Profile environment is ready.'",
    ),
]


def build_snapshot_profile(
    executor: CommandExecutor,
    description: str = "Snapshot of running machine services and containers.",
) -> ProfileConfig:
    """Capture currently active non-core system units, user units, containers, and ADB into a ProfileConfig."""
    discovered = discover_installed_services(executor)
    sys_running: list[str] = []
    usr_running: list[str] = []
    containers_running: list[str] = []
    compose_groups: dict[tuple[str, str, str], set[str]] = {}
    podman_running: list[str] = []
    vms_running: list[str] = []

    for item in discovered:
        state_lower = item.machine_state.lower()
        if item.category == "systemd_system" and not item.is_core_os:
            if state_lower.startswith("active"):
                sys_running.append(item.name)
        elif item.category == "systemd_user" and not is_core_os_unit(item.name):
            if state_lower.startswith("active"):
                usr_running.append(item.name)
        elif item.category == "docker_container" and state_lower.startswith("running"):
            if item.compose_project and item.compose_file:
                key = (
                    item.compose_project,
                    item.compose_file,
                    item.compose_working_dir or "",
                )
                compose_groups.setdefault(key, set())
                if item.compose_service:
                    compose_groups[key].add(item.compose_service)
            else:
                containers_running.append(item.name)
        elif item.category == "podman_container" and state_lower.startswith("running"):
            podman_running.append(item.name)
        elif item.category == "libvirt_vm" and "running" in state_lower:
            vms_running.append(item.name)

    projects = [key[0] for key in compose_groups]
    compose_stacks: list[DockerComposeStack] = []
    used_names: set[str] = set()
    for project, compose_file, working_dir in sorted(compose_groups):
        name = project
        if projects.count(project) > 1:
            project_path = working_dir or str(Path(compose_file).parent)
            name = f"{project}-{Path(project_path).name}"
        base_name = name
        suffix = 2
        while name in used_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        used_names.add(name)
        services = sorted(compose_groups[(project, compose_file, working_dir)])
        compose_stacks.append(
            DockerComposeStack(
                name=name,
                file=compose_file,
                project_dir=working_dir or None,
                services=services,
            )
        )

    adb_res = executor.run(["pgrep", "-x", "adb"], sudo=False, mutating=False)
    adb_active = adb_res.returncode == 0 and bool(adb_res.stdout.strip())
    docker_enabled = (
        "docker.service" in sys_running or bool(containers_running) or bool(compose_stacks)
    )

    return ProfileConfig(
        description=description,
        systemd=SystemdConfig(
            system=sys_running,
            user=usr_running,
            ensure_stopped=[],
        ),
        docker=DockerConfig(
            enabled=docker_enabled,
            containers=containers_running,
            compose_stacks=compose_stacks,
        ),
        podman=PodmanConfig(containers=podman_running),
        libvirt=LibvirtConfig(vms=vms_running),
        android=AndroidConfig(adb_server=adb_active),
    )
