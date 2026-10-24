"""Exclusive profile transition and diff engine for Linux Service Profiles (srvp)."""

from __future__ import annotations

from dataclasses import dataclass, field

from srvp.config import ProfileConfig, ProfilesFile
from srvp.executor import CommandExecutor
from srvp.providers import (
    ActionRecord,
    AdbProvider,
    DockerComposeProvider,
    DockerContainerProvider,
    Esp32SerialProvider,
    ServiceProvider,
    ServiceStatus,
    SystemdSystemProvider,
    SystemdUserProvider,
    run_hook_commands,
)
from srvp.state import AppState, ManagedItem, StateManager


@dataclass
class TransitionPlan:
    """Computed exclusive switch plan between the current state and target profile."""

    previous_profile: str | None
    target_profile: str | None
    pre_stop_hooks: list[str] = field(default_factory=list)
    items_to_stop: list[ManagedItem] = field(default_factory=list)
    post_stop_hooks: list[str] = field(default_factory=list)
    pre_start_hooks: list[str] = field(default_factory=list)
    items_to_start: list[ManagedItem] = field(default_factory=list)
    post_start_hooks: list[str] = field(default_factory=list)
    desired_active_items: list[ManagedItem] = field(default_factory=list)


@dataclass
class TransitionResult:
    """Outcome of executing (or dry-running) a TransitionPlan."""

    plan: TransitionPlan
    records: list[ActionRecord]
    dry_run: bool

    @property
    def success(self) -> bool:
        return all(r.success for r in self.records)


class ProfileEngine:
    """Orchestrates status inspection, exclusive diff computation, and profile switching."""

    def __init__(
        self,
        config: ProfilesFile,
        state_manager: StateManager,
        executor: CommandExecutor,
    ) -> None:
        self.config = config
        self.state_manager = state_manager
        self.executor = executor
        self.providers: list[ServiceProvider] = [
            SystemdSystemProvider(),
            SystemdUserProvider(),
            DockerContainerProvider(),
            DockerComposeProvider(),
            AdbProvider(),
            Esp32SerialProvider(),
        ]
        self._provider_map: dict[str, ServiceProvider] = {
            p.provider_name: p for p in self.providers
        }

    def collect_profile_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        """Collect all desired ManagedItems across all providers for `profile`."""
        items: list[ManagedItem] = []
        for provider in self.providers:
            items.extend(provider.get_desired_items(profile))
        return items

    def collect_explicit_stop_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        """Collect all explicitly forbidden ManagedItems (`ensure_stopped`) for `profile`."""
        items: list[ManagedItem] = []
        for provider in self.providers:
            items.extend(provider.get_explicit_stop_items(profile))
        return items

    def compute_transition_plan(self, target_profile_name: str | None) -> TransitionPlan:
        """Compute the exclusive diff between the currently active state and `target_profile_name`."""
        current_state: AppState = self.state_manager.load()
        prev_name = current_state.active_profile
        prev_profile = self.config.profiles.get(prev_name) if prev_name else None

        target_profile: ProfileConfig | None = None
        if target_profile_name is not None:
            if target_profile_name not in self.config.profiles:
                raise KeyError(f"Profile '{target_profile_name}' not found in configuration.")
            target_profile = self.config.profiles[target_profile_name]

        # Collect previously active items (both from state.json and from prev_profile definition)
        previous_items_by_key: dict[tuple[str, str], ManagedItem] = {}
        for item in current_state.active_items:
            previous_items_by_key[item.key] = item
        if prev_profile is not None:
            for item in self.collect_profile_items(prev_profile):
                previous_items_by_key.setdefault(item.key, item)

        desired_items = self.collect_profile_items(target_profile) if target_profile else []
        desired_keys = {it.key for it in desired_items}

        # 1. Exclusive diff: stop anything from previous profile not needed in target profile
        items_to_stop_map: dict[tuple[str, str], ManagedItem] = {}
        for key, item in previous_items_by_key.items():
            if key not in desired_keys:
                items_to_stop_map[key] = item

        # 2. Also include target profile's explicit ensure_stopped items
        if target_profile is not None:
            for item in self.collect_explicit_stop_items(target_profile):
                if item.key not in desired_keys:
                    items_to_stop_map[item.key] = item

        pre_stop = (
            list(prev_profile.hooks.pre_stop)
            if (prev_profile and prev_name != target_profile_name)
            else []
        )
        post_stop = (
            list(prev_profile.hooks.post_stop)
            if (prev_profile and prev_name != target_profile_name)
            else []
        )
        pre_start = list(target_profile.hooks.pre_start) if target_profile else []
        post_start = list(target_profile.hooks.post_start) if target_profile else []

        return TransitionPlan(
            previous_profile=prev_name,
            target_profile=target_profile_name,
            pre_stop_hooks=pre_stop,
            items_to_stop=list(items_to_stop_map.values()),
            post_stop_hooks=post_stop,
            pre_start_hooks=pre_start,
            items_to_start=desired_items,
            post_start_hooks=post_start,
            desired_active_items=desired_items,
        )

    def execute_plan(self, plan: TransitionPlan) -> TransitionResult:
        """Execute a TransitionPlan across all providers and update state (unless dry_run)."""
        records: list[ActionRecord] = []

        # 1. Pre-stop hooks
        records.extend(run_hook_commands("pre_stop", plan.pre_stop_hooks, self.executor))

        # 2. Stop items in reverse provider order (containers/compose before docker.service)
        for provider in reversed(self.providers):
            prov_stop_items = [
                it for it in plan.items_to_stop if it.provider == provider.provider_name
            ]
            if prov_stop_items:
                records.extend(provider.stop_items(prov_stop_items, self.executor))

        # 3. Post-stop hooks
        records.extend(run_hook_commands("post_stop", plan.post_stop_hooks, self.executor))

        # 4. Pre-start hooks
        records.extend(run_hook_commands("pre_start", plan.pre_start_hooks, self.executor))

        # 5. Start items in forward provider order (systemd/docker.service before containers/compose)
        for provider in self.providers:
            prov_start_items = [
                it for it in plan.items_to_start if it.provider == provider.provider_name
            ]
            if prov_start_items:
                records.extend(provider.start_items(prov_start_items, self.executor))

        # 6. Post-start hooks
        records.extend(run_hook_commands("post_start", plan.post_start_hooks, self.executor))

        if not self.executor.is_dry_run:
            self.state_manager.record_activation(plan.target_profile, plan.desired_active_items)

        return TransitionResult(
            plan=plan,
            records=records,
            dry_run=self.executor.is_dry_run,
        )

    def switch_profile(self, target_profile_name: str) -> TransitionResult:
        """Compute and execute an exclusive switch to `target_profile_name`."""
        plan = self.compute_transition_plan(target_profile_name)
        return self.execute_plan(plan)

    def stop_all(self) -> TransitionResult:
        """Deactivate the currently active profile and stop all its managed services."""
        plan = self.compute_transition_plan(None)
        return self.execute_plan(plan)

    def get_profile_statuses(self, profile_name: str | None = None) -> list[ServiceStatus]:
        """Return live ServiceStatus objects for a specific profile or all configured items."""
        items_map: dict[tuple[str, str], ManagedItem] = {}
        if profile_name and profile_name in self.config.profiles:
            prof = self.config.profiles[profile_name]
            for it in self.collect_profile_items(prof):
                items_map[it.key] = it
            for it in self.collect_explicit_stop_items(prof):
                items_map.setdefault(it.key, it)
        else:
            state = self.state_manager.load()
            for it in state.active_items:
                items_map[it.key] = it
            for prof in self.config.profiles.values():
                for it in self.collect_profile_items(prof):
                    items_map.setdefault(it.key, it)
                for it in self.collect_explicit_stop_items(prof):
                    items_map.setdefault(it.key, it)

        statuses: list[ServiceStatus] = []
        for item in items_map.values():
            provider = self._provider_map.get(item.provider)
            if provider is not None:
                statuses.append(provider.get_status(item, self.executor))
        return statuses

    def validate_config(self) -> list[str]:
        """Validate profile configuration semantics (e.g., checking Docker Compose file paths)."""
        warnings: list[str] = []
        for prof_name, prof in self.config.profiles.items():
            overlap = set(prof.effective_system_units()) & set(
                prof.effective_ensure_stopped_units()
            )
            if overlap:
                warnings.append(
                    f"Profile '{prof_name}' lists {sorted(overlap)} in both system start and ensure_stopped."
                )
            for stack in prof.docker.compose_stacks:
                if not self.executor.file_exists(stack.file):
                    warnings.append(
                        f"Profile '{prof_name}' compose stack '{stack.name}' references missing file '{stack.file}'."
                    )
        return warnings
