"""Multi-account switcher for Codex."""

from importlib.metadata import version

__version__ = version("codex-swap")

from codex_swap.switcher import CodexAccountSwitcher

__all__ = ["CodexAccountSwitcher", "__version__"]
