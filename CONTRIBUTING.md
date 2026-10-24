# Contributing to Linux Service Profiles (`srvp`)

Thank you for your interest in contributing to `srvp`!

## Development Environment

1. Create a virtual environment inside the repository root:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e ".[dev]"
   ```
   *(Or use `uv sync` if `uv` is installed.)*

2. Run the automated verification suite:
   ```bash
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   .venv/bin/mypy src tests
   .venv/bin/pytest -v --cov=srvp
   ```

## Adding a New Service Provider

1. Define any new configuration fields on `ProfileConfig` in `src/srvp/config.py`.
2. Implement a `ServiceProvider` subclass in `src/srvp/providers/` using the injected `CommandExecutor` protocol (`src/srvp/executor.py`). Never invoke `subprocess.run` directly inside a provider so `--dry-run` and `MockCommandExecutor` remain deterministic.
3. Register the provider in `ProfileEngine` (`src/srvp/engine.py`).
4. Expose new fields in the TUI Profile Editor (`src/srvp/tui/editor.py`).
5. Add unit tests in `tests/test_providers.py` and headless TUI tests in `tests/test_tui.py`.
