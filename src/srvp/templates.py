"""Built-in starter profile templates for srvp init."""

from __future__ import annotations

from pathlib import Path

from srvp.config import (
    AndroidConfig,
    DockerConfig,
    Esp32SerialConfig,
    HooksConfig,
    ProfileConfig,
    ProfilesFile,
    SystemdConfig,
)


def detect_default_redis_unit() -> str:
    """Detect whether the host uses 'redis.service' (Fedora/Arch/openSUSE) or 'redis-server.service' (Debian/Ubuntu)."""
    debian_paths = (
        Path("/lib/systemd/system/redis-server.service"),
        Path("/usr/lib/systemd/system/redis-server.service"),
    )
    generic_paths = (
        Path("/usr/lib/systemd/system/redis.service"),
        Path("/lib/systemd/system/redis.service"),
    )
    if any(p.exists() for p in debian_paths):
        return "redis-server.service"
    if any(p.exists() for p in generic_paths):
        return "redis.service"
    return "redis-server.service"


def build_starter_profiles(redis_unit: str | None = None) -> ProfilesFile:
    """Return the default starter ProfilesFile covering common Linux laptop workflows."""
    resolved_redis = redis_unit or detect_default_redis_unit()
    return ProfilesFile(
        version=1,
        profiles={
            "web-db": ProfileConfig(
                description="Full-stack web development with local PostgreSQL and Redis.",
                systemd=SystemdConfig(
                    system=["postgresql.service", resolved_redis],
                    user=[],
                    ensure_stopped=["docker.service"],
                ),
                hooks=HooksConfig(
                    post_start=["echo 'Web & DB services are ready (PostgreSQL + Redis).'"],
                ),
            ),
            "docker-dev": ProfileConfig(
                description="Containerized development using Docker and Docker Compose.",
                systemd=SystemdConfig(
                    system=["docker.service", "containerd.service"],
                    user=[],
                    ensure_stopped=["postgresql.service", resolved_redis],
                ),
                docker=DockerConfig(
                    enabled=True,
                    containers=[],
                    compose_stacks=[],
                ),
                hooks=HooksConfig(
                    post_start=[
                        "echo 'Docker daemon active; local host DB daemons stopped to avoid port clashes.'"
                    ],
                ),
            ),
            "android-dev": ProfileConfig(
                description="Android Studio / SDK workflow with ADB server and freed host memory.",
                systemd=SystemdConfig(
                    system=[],
                    user=[],
                    ensure_stopped=[
                        "postgresql.service",
                        resolved_redis,
                        "docker.service",
                    ],
                ),
                android=AndroidConfig(adb_server=True),
                hooks=HooksConfig(
                    post_start=[
                        "echo 'ADB server started; heavy background daemons stopped for emulator RAM.'"
                    ],
                ),
            ),
            "esp32-embedded": ProfileConfig(
                description="ESP32 / microcontroller development with serial port conflict protection.",
                systemd=SystemdConfig(
                    system=[],
                    user=[],
                    ensure_stopped=["brltty.service", "ModemManager.service", "docker.service"],
                ),
                esp32_serial=Esp32SerialConfig(
                    enabled=True,
                    check_dialout_group=True,
                    stop_conflicting_services=True,
                    ports_glob=["/dev/ttyUSB*", "/dev/ttyACM*"],
                ),
                hooks=HooksConfig(
                    post_start=[
                        "echo 'ESP32 serial environment ready (brltty & ModemManager stopped).'"
                    ],
                ),
            ),
            "idle": ProfileConfig(
                description="Battery saver / daily driver profile with all dev daemons stopped.",
                systemd=SystemdConfig(
                    system=[],
                    user=[],
                    ensure_stopped=[
                        "postgresql.service",
                        resolved_redis,
                        "docker.service",
                        "containerd.service",
                    ],
                ),
                android=AndroidConfig(adb_server=False),
                hooks=HooksConfig(
                    post_start=[
                        "echo 'Switched to idle profile. Background dev services stopped.'"
                    ],
                ),
            ),
        },
    )
