import pytest

from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.tui.app import CswapApp


@pytest.mark.asyncio
async def test_dashboard_and_watch_mount():
    app = CswapApp(CodexAccountSwitcher(), start="watch")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == "WatchScreen"
        await pilot.press("escape")
        assert type(app.screen).__name__ == "DashboardScreen"
