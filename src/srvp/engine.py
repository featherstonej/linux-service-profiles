"""Exclusive profile transition and diff engine for Linux Service Profiles (srvp)."""

from __future__ import annotations

from dataclasses import dataclass, field

from srvp.config import ProfileConfig, ProfilesFile, resolve_compose_paths
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

        # Collect previously active items from the recorded activation snapshot (state.json),
        # keyed by full resource_key (provider, identifier, metadata) so modified resources (e.g.
        # Compose stacks with the same name but different compose file) are stopped with their old
        # metadata before replacement.
        previous_items_by_rkey: dict[tuple[str, str, tuple[tuple[str, str], ...]], ManagedItem] = {
            item.resource_key: item for item in current_state.active_items
        }

        desired_items = self.collect_profile_items(target_profile) if target_profile else []
        desired_rkeys = {it.resource_key for it in desired_items}

        # 1. Exclusive diff: stop anything from previous state/profile not matching desired_rkeys
        items_to_stop_map: dict[tuple[str, str, tuple[tuple[str, str], ...]], ManagedItem] = {}
        for rkey, item in previous_items_by_rkey.items():
            if rkey not in desired_rkeys:
                items_to_stop_map[rkey] = item

        # 2. Also include target profile's explicit ensure_stopped items
        if target_profile is not None:
            for item in self.collect_explicit_stop_items(target_profile):
                if item.resource_key not in desired_rkeys:
                    items_to_stop_map.setdefault(item.resource_key, item)

        if prev_name != target_profile_name:
            if prev_profile is not None:
                pre_stop = list(prev_profile.hooks.pre_stop)
                post_stop = list(prev_profile.hooks.post_stop)
            elif prev_name is not None:
                pre_stop = list(current_state.pre_stop_hooks)
                post_stop = list(current_state.post_stop_hooks)
            else:
                pre_stop = []
                post_stop = []
        else:
            pre_stop = []
            post_stop = []

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

    @staticmethod
    def _match_item_outcomes(
        items: list[ManagedItem],
        records: list[ActionRecord],
        *,
        default_when_empty: bool = False,
    ) -> list[tuple[ManagedItem, bool]]:
        """Map each ManagedItem in `items` to whether its provider action succeeded."""
        if not records:
            return [(it, default_when_empty) for it in items]
        if len(records) == len(items) and all(
            r.identifier == it.identifier for r, it in zip(records, items, strict=True)
        ):
            return [(it, r.success) for it, r in zip(items, records, strict=True)]
        by_ident: dict[str, bool] = {}
        for r in records:
            if r.identifier not in by_ident:
                by_ident[r.identifier] = r.success
            else:
                by_ident[r.identifier] = by_ident[r.identifier] and r.success
        return [(it, by_ident.get(it.identifier, False)) for it in items]

    def execute_plan(self, plan: TransitionPlan) -> TransitionResult:
        """Execute a TransitionPlan across all providers and update state (unless dry_run)."""
        with self.state_manager.transition_lock():
            return self._execute_plan_locked(plan)

    def _execute_plan_locked(self, plan: TransitionPlan) -> TransitionResult:
        records: list[ActionRecord] = []
        current_state: AppState = self.state_manager.load()

        # Seed tracked active items from the recorded activation snapshot
        tracked_items: dict[tuple[str, str, tuple[tuple[str, str], ...]], ManagedItem] = {
            it.resource_key: it for it in current_state.active_items
        }

        stop_phase_failed = False
        start_phase_failed = False

        # 1. Pre-stop hooks
        if plan.pre_stop_hooks:
            pre_stop_recs = run_hook_commands("pre_stop", plan.pre_stop_hooks, self.executor)
            records.extend(pre_stop_recs)
            if any(not r.success for r in pre_stop_recs):
                stop_phase_failed = True

        # 2. Stop items in reverse provider order (containers/compose before docker.service)
        if not stop_phase_failed:
            for provider in reversed(self.providers):
                prov_stop_items = [
                    it for it in plan.items_to_stop if it.provider == provider.provider_name
                ]
                if not prov_stop_items:
                    continue
                prov_recs = provider.stop_items(prov_stop_items, self.executor)
                records.extend(prov_recs)
                # Esp32SerialProvider.stop_items is a no-op returning []
                outcomes = self._match_item_outcomes(
                    prov_stop_items,
                    prov_recs,
                    default_when_empty=(len(prov_recs) == 0),
                )
                for it, ok in outcomes:
                    if ok:
                        tracked_items.pop(it.resource_key, None)
                    else:
                        tracked_items[it.resource_key] = it
                        stop_phase_failed = True
                if any(not r.success for r in prov_recs) or stop_phase_failed:
                    stop_phase_failed = True
                    break

        # 3. Post-stop hooks
        if not stop_phase_failed and plan.post_stop_hooks:
            post_stop_recs = run_hook_commands("post_stop", plan.post_stop_hooks, self.executor)
            records.extend(post_stop_recs)
            if any(not r.success for r in post_stop_recs):
                stop_phase_failed = True

        # 4. Pre-start hooks
        if not stop_phase_failed and plan.pre_start_hooks:
            pre_start_recs = run_hook_commands("pre_start", plan.pre_start_hooks, self.executor)
            records.extend(pre_start_recs)
            if any(not r.success for r in pre_start_recs):
                start_phase_failed = True

        # 5. Start items in forward provider order (systemd/docker.service before containers/compose)
        if not stop_phase_failed and not start_phase_failed:
            for provider in self.providers:
                prov_start_items = [
                    it for it in plan.items_to_start if it.provider == provider.provider_name
                ]
                if not prov_start_items:
                    continue
                prov_recs = provider.start_items(prov_start_items, self.executor)
                records.extend(prov_recs)
                outcomes = self._match_item_outcomes(
                    prov_start_items,
                    prov_recs,
                    default_when_empty=False,
                )
                for it, ok in outcomes:
                    if ok:
                        tracked_items[it.resource_key] = it
                    else:
                        tracked_items.pop(it.resource_key, None)
                        start_phase_failed = True
                if any(not r.success for r in prov_recs) or start_phase_failed:
                    start_phase_failed = True
                    break

        # 6. Post-start hooks
        if not stop_phase_failed and not start_phase_failed and plan.post_start_hooks:
            post_start_recs = run_hook_commands("post_start", plan.post_start_hooks, self.executor)
            records.extend(post_start_recs)
            if any(not r.success for r in post_start_recs):
                start_phase_failed = True

        if not self.executor.is_dry_run:
            if not stop_phase_failed and not start_phase_failed:
                target_prof_obj = (
                    self.config.profiles.get(plan.target_profile) if plan.target_profile else None
                )
                self.state_manager.record_activation(
                    plan.target_profile,
                    list(plan.desired_active_items),
                    pre_stop_hooks=target_prof_obj.hooks.pre_stop if target_prof_obj else [],
                    post_stop_hooks=target_prof_obj.hooks.post_stop if target_prof_obj else [],
                )
            elif stop_phase_failed:
                # Retain previous profile activation and any items that remain running
                self.state_manager.save(
                    AppState(
                        active_profile=current_state.active_profile,
                        activated_at=current_state.activated_at,
                        active_items=list(tracked_items.values()),
                        pre_stop_hooks=current_state.pre_stop_hooks,
                        post_stop_hooks=current_state.post_stop_hooks,
                    )
                )
            else:
                # Stop phase completed, but target profile startup/hooks failed:
                # do not mark target profile active, but retain all running/started items.
                self.state_manager.save(
                    AppState(
                        active_profile=None,
                        activated_at=None,
                        active_items=list(tracked_items.values()),
                        pre_stop_hooks=[],
                        post_stop_hooks=[],
                    )
                )

        return TransitionResult(
            plan=plan,
            records=records,
            dry_run=self.executor.is_dry_run,
        )

    def switch_profile(self, target_profile_name: str) -> TransitionResult:
        """Compute and execute an exclusive switch to `target_profile_name`."""
        with self.state_manager.transition_lock():
            plan = self.compute_transition_plan(target_profile_name)
            return self.execute_plan(plan)

    def stop_all(self) -> TransitionResult:
        """Deactivate the currently active profile and stop all its managed services."""
        with self.state_manager.transition_lock():
            plan = self.compute_transition_plan(None)
            return self.execute_plan(plan)

    def get_profile_statuses(self, profile_name: str | None = None) -> list[ServiceStatus]:
        """Return live ServiceStatus objects for a specific profile or all configured items."""
        items_map: dict[tuple[str, str, tuple[tuple[str, str], ...]], ManagedItem] = {}
        if profile_name and profile_name in self.config.profiles:
            prof = self.config.profiles[profile_name]
            for it in self.collect_profile_items(prof):
                items_map[it.resource_key] = it
            for it in self.collect_explicit_stop_items(prof):
                items_map.setdefault(it.resource_key, it)
        else:
            state = self.state_manager.load()
            for it in state.active_items:
                items_map[it.resource_key] = it
            for prof in self.config.profiles.values():
                for it in self.collect_profile_items(prof):
                    items_map.setdefault(it.resource_key, it)
                for it in self.collect_explicit_stop_items(prof):
                    items_map.setdefault(it.resource_key, it)

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
                resolved_file, _ = resolve_compose_paths(stack.file, stack.project_dir)
                if not self.executor.file_exists(resolved_file):
                    warnings.append(
                        f"Profile '{prof_name}' compose stack '{stack.name}' references missing file '{stack.file}'."
                    )
        return warnings
