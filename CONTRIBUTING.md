# Contributing

## Dependencies

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and `pyproject.toml`. From the project root:

```bash
# Install production dependencies
uv sync

# Install with dev dependencies (e.g. pytest)
uv sync --extra dev
```

Run the webhook scripts or tests in the uv environment:

```bash
uv run python -m scripts.bootstrap_webhook --help
uv run pytest tests/ -v
```

## Format and lint

CI uses the same commands. Use tox so the same Ruff version as CI runs:

```bash
tox -e fmt    # format with ruff 0.15.6
tox -e lint   # verify, must pass before pushing
```

If `tox -e fmt` changes any file, commit those changes so CI passes. Do not rely on `ruff format` from your venv; it may be a different version.

### Automatic formatting on commit

Install the pre-commit hook so Ruff formats `app.py` and `scripts/*.py` before each commit:

```bash
uv sync --extra dev
pre-commit install
```

Run `pre-commit run --all-files` once to format everything.
