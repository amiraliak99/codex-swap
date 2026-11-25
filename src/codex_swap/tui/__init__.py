"""Textual-based interactive TUI for codex-swap.

Entry point for ``codexswap tui`` (and bare ``codexswap`` in an interactive
terminal). Heavy imports (textual, rich) stay inside :func:`run` so the
plain CLI paths — ``codexswap list``, cron's ``codexswap auto --once`` — never pay
for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_swap.switcher import CodexAccountSwitcher


def run(switcher: "CodexAccountSwitcher", start: str = "dashboard") -> int:
    """Run the TUI over an existing switcher. Returns the process exit code.

    ``start="watch"`` (the ``codexswap watch`` command) opens directly on the
    live watch page, stacked over the dashboard.
    """
    from codex_swap.appearance import detect_terminal_background, drain_stdin
    from codex_swap.tui.app import CswapApp

    # Sense the terminal background while we still own stdin in cooked mode
    # (Textual's driver starts inside app.run()). Always detect so cycling to
    # 'auto' works even when the initial theme is explicit. Both calls are
    # meant to fail safe on their own, but they're wrapped here too: a
    # detection bug must never crash the TUI launch.
    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = CswapApp(switcher, start=start, detected=detected)
    # Drain any late OSC reply immediately before Textual's driver starts,
    # so it isn't reissued as keystrokes once the app takes over the terminal.
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0
