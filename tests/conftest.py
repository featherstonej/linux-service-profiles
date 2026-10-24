"""Shared pytest fixtures for Linux Service Profiles (srvp)."""

from __future__ import annotations

from pathlib import Path

import pytest

from srvp.config import ProfilesFile, save_profiles
from srvp.executor import MockCommandExecutor
from srvp.state import StateManager
from srvp.templates import build_starter_profiles


@pytest.fixture()
def tmp_config_path(tmp_path: Path) -> Path:
    """Return an isolated temporary path for profiles.yaml initialized with starter templates."""
    cfg_path = tmp_path / "config" / "profiles.yaml"
    save_profiles(build_starter_profiles(), cfg_path)
    return cfg_path


@pytest.fixture()
def tmp_state_path(tmp_path: Path) -> Path:
    """Return an isolated temporary path for state.json."""
    return tmp_path / "state" / "state.json"


@pytest.fixture()
def state_manager(tmp_state_path: Path) -> StateManager:
    """Return a StateManager bound to tmp_state_path."""
    return StateManager(tmp_state_path)


@pytest.fixture()
def mock_executor(tmp_path: Path) -> MockCommandExecutor:
    """Return a fresh MockCommandExecutor backed by a temporary state file."""
    return MockCommandExecutor(state_file=tmp_path / "state" / "mock_system.json")


@pytest.fixture()
def starter_profiles() -> ProfilesFile:
    """Return a fresh copy of the starter ProfilesFile."""
    return build_starter_profiles()
