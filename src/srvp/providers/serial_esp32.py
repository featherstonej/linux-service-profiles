"""ESP32 & microcontroller serial port readiness and conflict check provider."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


class Esp32SerialProvider(ServiceProvider):
    """Checks serial ports (/dev/ttyUSB*, /dev/ttyACM*), dialout group membership, and conflicts."""

    provider_name = "esp32_serial"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        if not profile.esp32_serial.enabled:
            return []
        return [
            ManagedItem(
                provider=self.provider_name,
                identifier="esp32-serial",
                metadata={
                    "check_dialout": str(profile.esp32_serial.check_dialout_group),
                    "ports_glob": ",".join(profile.esp32_serial.ports_glob),
                },
            )
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        patterns = [
            p for p in item.metadata.get("ports_glob", "/dev/ttyUSB*,/dev/ttyACM*").split(",") if p
        ]
        detected_ports: list[str] = []
        for pat in patterns:
            for found in executor.glob_paths(pat):
                if found not in detected_ports:
                    detected_ports.append(found)

        groups = executor.get_user_groups()
        matched_group = next((g for g in ("dialout", "uucp") if g in groups), None)
        has_serial_group = matched_group is not None
        check_dialout = item.metadata.get("check_dialout", "True").lower() == "true"

        ports_summary = ", ".join(detected_ports) if detected_ports else "no board plugged in"
        if matched_group:
            group_summary = f"{matched_group} OK"
        elif not check_dialout:
            group_summary = "group check skipped"
        else:
            group_summary = (
                "MISSING dialout/uucp group (run: sudo usermod -aG dialout $USER or uucp on Arch)"
            )
        ready = has_serial_group or not check_dialout

        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=ready,
            status_text="ready" if ready else "group-warn",
            pid=None,
            cpu_percent=0.0,
            memory_mb=0.0,
            detail=f"Ports: {ports_summary} | {group_summary}",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            status = self.get_status(item, executor)
            records.append(
                ActionRecord(
                    action="check",
                    provider=self.provider_name,
                    identifier=item.identifier,
                    command="serial-port-check",
                    success=True,
                    message=status.detail,
                    dry_run=executor.is_dry_run,
                )
            )
        return records

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        return []
