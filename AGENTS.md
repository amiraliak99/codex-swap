# Working on codex-swap

Read [docs/architecture.md](docs/architecture.md) first. The tool reads and moves credentials. Go through `credentials.py` and `paths.py`. Never print or log tokens, API keys or credential-file contents. Check identity before checkpointing a grant. Borrow Codex behavior only from versioned source, generated app-server schema or a measured check.

Run `uv sync --locked` and `uv run pytest`. Tests use temporary homes and must keep their guards on. Match the existing code style and add focused regression tests for changes to credential ownership. Keep JSON output additive.

Use the README for quick starts, `docs/` for user-facing behavior, and module docstrings for implementation details.
