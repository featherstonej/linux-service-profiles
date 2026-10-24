# Linux Service Profiles (`srvp`)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**`srvp`** (**Linux Service Profiles**) is an open-source CLI and interactive Terminal UI (TUI) tool for Linux laptops and workstations. It lets you switch between customizable development profiles—automatically starting the services you need and stopping heavy background daemons you don't.

---

## Why `srvp`?

Polyglot developers often use a single Linux machine for web backends, Dockerized microservices, Android apps, and embedded/ESP32 projects. Leaving PostgreSQL, Redis, Docker, ADB, and serial monitors running at all times:
- **Drains laptop battery and RAM**
- **Causes port clashes** (e.g., local PostgreSQL vs. Docker Compose PostgreSQL on `:5432`)
- **Causes hardware conflicts** (e.g., `brltty` or `ModemManager` hijacking `/dev/ttyUSB0` when flashing an ESP32 board)

With `srvp`, switching contexts is a single command or keypress:
```bash
srvp switch esp32-embedded
```

---

## Key Features

- **Interactive Terminal UI (`Textual`)**: Live dashboard (`srvp` or `srvp ui`) showing active profile, service status (`active`/`inactive`), PID, CPU %, and RAM (MB), plus an **in-app Profile Editor** with an **Installed Service & Container Picker** to browse services installed on your machine and mark them Active (Start) or Inactive (Stop).
- **Exclusive Profile Switching**: Tracks which services were started by the previous profile in `~/.local/state/linux-service-profiles/state.json` and stops unneeded services before starting the new profile's services.
- **Multi-Provider Support**:
  - **Systemd**: System units (`systemctl`) and User units (`systemctl --user`), plus explicit `ensure_stopped` conflict lists.
  - **Docker & Docker Compose**: Manages `docker.service`, standalone containers, and `docker compose -f <file> up -d / down` stacks.
  - **Podman & Podman Compose**: Manages rootless/rootful `podman` containers and `podman compose -f <file> up -d / down` stacks.
  - **Libvirt / KVM Virtual Machines**: Starts and gracefully shuts down `virsh` VMs (`libvirtd.service` automatically managed).
  - **Android (ADB)**: Starts and stops the Android Debug Bridge daemon (`adb start-server` / `adb kill-server`).
  - **ESP32 / Serial Diagnostics**: Verifies `/dev/ttyUSB*` & `/dev/ttyACM*` device nodes, checks serial group membership (`dialout` on Debian/Ubuntu/Fedora/openSUSE or `uucp` on Arch), and automatically stops conflicting `brltty` and `ModemManager` daemons.
  - **Custom Shell Hooks**: Run `pre_start`, `post_start`, `pre_stop`, and `post_stop` commands on profile transitions.
- **Automation & Pre-Flight Diagnostics**:
  - **Directory Auto-Switching (`.srvp`)**: Place a `.srvp` file in any project directory and use `srvp auto` (or `eval "$(srvp auto --shell-hook bash)"`) to switch profiles automatically on `cd`.
  - **Port Conflict Doctor (`srvp doctor`)**: Detects unmanaged processes occupying TCP ports needed by a profile (e.g., `:5432`, `:6379`) and warns in both the CLI and TUI transition preview.
  - **Battery Power Saver (`srvp power-check`)**: Automatically switches to `idle` when on battery below a configured threshold, or generates a `udev` rule (`--udev-rule`).
  - **Shareable Profile Packs (`srvp export` / `srvp import`)**: Export profiles to YAML files for team repositories and import/merge them with validation.
- **Batched `sudo` & Polkit Generator**: Batches privileged `systemctl` calls so you only enter your password once per switch, or generates a Polkit rule (`srvp polkit-setup`) for passwordless switching.
- **Safe `--dry-run` & `--mock` Modes**: Preview every command before executing, or simulate switches in memory with isolated mock activation state (`mock_state.json`).

---

## Installation

### Using `uv` (Recommended)
```bash
uv tool install .
```

### Development Setup
```bash
git clone https://github.com/featherstonej/linux-service-profiles.git
cd linux-service-profiles
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

---

## Quickstart

1. **Initialize Starter Profiles** (`~/.config/linux-service-profiles/profiles.yaml`):
   ```bash
   srvp init
   ```
   Starter templates included out of the box:
   - `web-db`: Local PostgreSQL & Redis (`docker.service` stopped)
   - `docker-dev`: Docker & Containerd (`postgresql` & `redis-server` stopped)
   - `android-dev`: ADB server started; heavy DB/Docker daemons stopped for Android Emulator RAM
   - `esp32-embedded`: Serial port checks enabled; `brltty` & `ModemManager` stopped
   - `idle`: Battery saver mode with all development daemons stopped

2. **Launch the Interactive TUI**:
   ```bash
   srvp
   # or: srvp ui
   ```
   - Use **`↑` / `↓`** to browse profiles and preview transition diffs.
   - Press **`s`** to switch to the highlighted profile.
   - Press **`d`** to run a Dry-Run preview.
   - Press **`x`** to stop all active profile services (Idle).
   - Press **`n`** / **`e`** / **`c`** / **`m`** / **`Delete`** to create, edit, clone, snapshot running services, or delete profiles in the built-in Profile Editor.
   - Press **`p`** to pause or resume live 4-second telemetry auto-refresh.
   - Inside the Profile Editor, click **"Select..."** next to any field to search installed systemd units, Docker containers, Docker Compose stacks, or shell hook presets.

3. **Use CLI Subcommands**:
   ```bash
   srvp list                          # List all profiles and active state
   srvp switch web-db --dry-run       # Preview what will stop and start
   srvp switch web-db                 # Activate the web-db profile
   srvp status                        # Show live service status, PIDs, CPU %, and RAM
   srvp snapshot my-current-stack     # Save currently running services/containers as a new profile
   srvp doctor --profile web-db       # Check for TCP port conflicts and serial group readiness
   srvp auto                          # Switch profile based on .srvp in current/parent directory
   srvp power-check --threshold 25    # Switch to idle if discharging at or below 25% battery
   srvp export web-db -o web-db.yaml  # Export a profile to a shareable YAML pack
   srvp import web-db.yaml            # Import and merge a shared YAML profile pack
   srvp stop                          # Stop active profile services (return to idle)
   srvp validate                      # Validate profiles.yaml syntax and paths
   srvp --install-completion          # Install dynamic shell tab-completion (bash/zsh/fish)
   ```

4. **Optional Passwordless Switching (Polkit)**:
   ```bash
   srvp polkit-setup
   # Supports both 'sudo' (Debian/Ubuntu) and 'wheel' (Fedora/RHEL/Arch/openSUSE) by default,
   # or pass --group <group-name> for a custom group.
   # Save output to /etc/polkit-1/rules.d/50-srvp-systemd.rules
   ```

---

## Configuration Reference (`profiles.yaml`)

Profiles are stored in `~/.config/linux-service-profiles/profiles.yaml` (override with `--config` or `SRVP_CONFIG_PATH`). See [`examples/profiles.example.yaml`](examples/profiles.example.yaml) for a full example:

```yaml
version: 1
profiles:
  web-db:
    description: Full-stack web development with local PostgreSQL and Redis.
    systemd:
      system:
        - postgresql.service
        - redis-server.service
      user: []
      ensure_stopped:
        - docker.service
    docker:
      enabled: false
      containers: []
      compose_stacks: []
    android:
      adb_server: false
    esp32_serial:
      enabled: false
    hooks:
      pre_start: []
      post_start:
        - "echo 'Web & DB services are ready.'"
      pre_stop: []
      post_stop: []
```

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
