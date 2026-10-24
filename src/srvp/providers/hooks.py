"""Custom shell hooks runner (pre_start, post_start, pre_stop, post_stop)."""

from __future__ import annotations

from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord


def run_hook_commands(
    stage: str, commands: list[str], executor: CommandExecutor
) -> list[ActionRecord]:
    """Execute a list of shell hook commands for a given lifecycle stage."""
    records: list[ActionRecord] = []
    for idx, cmd_str in enumerate(commands, start=1):
        res = executor.run([cmd_str], sudo=False, shell=True, mutating=True)
        records.append(
            ActionRecord(
                action="hook",
                provider=f"hook:{stage}",
                identifier=f"{stage}[{idx}]",
                command=cmd_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
        )
    return records
