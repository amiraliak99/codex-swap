import asyncio
import json
import os

import pytest

from codex_swap import app_server, live_session
from codex_swap.credentials import identify, read_auth, write_auth
from codex_swap.exceptions import SessionError
from codex_swap.switcher import CodexAccountSwitcher
from tests.conftest import login_record


@pytest.fixture
def live_fleet():
    switcher = CodexAccountSwitcher()
    first = login_record("first", workspace="workspace-a")
    second = login_record("second", workspace="workspace-b")
    write_auth(switcher.live_path, first)
    switcher.add_account(alias="work")
    switcher.register_auth(second, alias="personal")
    return switcher, first, second


@pytest.mark.asyncio
async def test_switch_transfers_owner_only_after_rpc_ack(live_fleet, monkeypatch):
    sw, first, second = live_fleet
    old = live_session._acquire_grant(sw, "work")
    bridge = live_session._Bridge(sw, old, None)
    bridge.ready = True
    first_key, second_key = identify(first).key, identify(second).key

    async def request(method, payload):
        assert method == "account/login/start"
        assert payload["chatgptAccountId"] == "workspace-b"
        assert "refreshToken" not in payload
        assert sw.session_busy(first_key)
        assert sw.session_busy(second_key)
        return {"type": "chatgptAuthTokens"}

    monkeypatch.setattr(bridge, "_request", request)
    try:
        assert "second@example.test" in await bridge._switch("personal")
        assert not sw.session_busy(first_key)
        assert sw.session_busy(second_key)
        assert read_auth(sw.live_path) == first
    finally:
        bridge.current.lease.release()


@pytest.mark.asyncio
async def test_rejected_switch_preserves_the_original_account(live_fleet, monkeypatch):
    sw, first, second = live_fleet
    current = live_session._acquire_grant(sw, "work")
    bridge = live_session._Bridge(sw, current, None)
    bridge.ready = True

    async def reject(_tokens):
        raise SessionError("rejected")

    monkeypatch.setattr(bridge, "_login", reject)
    try:
        with pytest.raises(SessionError, match="rejected"):
            await bridge._switch("personal")
        assert bridge.current is current
        assert sw.session_busy(identify(first).key)
        assert not sw.session_busy(identify(second).key)
    finally:
        current.lease.release()


@pytest.mark.asyncio
async def test_in_flight_turn_and_approval_block_a_switch(live_fleet):
    sw, _, _ = live_fleet
    current = live_session._acquire_grant(sw, "work")
    bridge = live_session._Bridge(sw, current, None)
    bridge.ready = True
    try:
        bridge.pending[4] = "turn/start"
        bridge._record_turn({"id": 4, "result": {"turn": {"id": "turn-a", "status": "inProgress"}}})
        with pytest.raises(SessionError, match="busy"):
            await bridge._switch("personal")
        bridge._record_turn({"method": "turn/completed", "params": {"turn": {"id": "turn-a"}}})
        bridge.pending_tools.add(9)
        with pytest.raises(SessionError, match="busy"):
            await bridge._switch("personal")
        bridge.pending_tools.clear()
        assert "Already using" in await bridge._switch("work")
    finally:
        current.lease.release()


def test_fast_turn_completion_before_start_response():
    bridge = live_session._Bridge(None, None, None)
    bridge.pending[4] = "turn/start"
    bridge._record_turn({"method": "turn/started", "params": {"turn": {"id": "turn-a"}}})
    bridge._record_turn({"method": "turn/completed", "params": {"turn": {"id": "turn-a"}}})
    bridge._record_turn({"id": 4, "result": {"turn": {"id": "turn-a", "status": "inProgress"}}})
    assert not bridge.pending
    assert not bridge.turns


def test_managed_refresh_returns_access_token_and_preserves_grant(live_fleet, monkeypatch):
    sw, _, second = live_fleet
    grant = live_session._acquire_grant(sw, "personal")
    renewed = login_record("second", workspace="workspace-b", generation="2")

    def renew(home):
        assert sw.session_busy(identify(second).key)
        write_auth(home / "auth.json", renewed)

    monkeypatch.setattr(app_server, "refresh", renew)
    try:
        tokens = grant.tokens(refresh=True)
        assert tokens["accessToken"] == renewed["tokens"]["access_token"]
        assert "refreshToken" not in tokens
        assert read_auth(grant.path) == renewed
    finally:
        grant.lease.release()


@pytest.mark.skipif(os.name == "nt", reason="Live control uses a Unix socket")
@pytest.mark.asyncio
async def test_second_terminal_can_send_a_switch(live_fleet, monkeypatch):
    import tempfile
    from pathlib import Path

    sw, _, _ = live_fleet
    bridge = live_session._Bridge(sw, None, None)

    async def switch(identifier):
        assert identifier == "personal"
        return "Switched"

    monkeypatch.setattr(bridge, "_switch", switch)
    with tempfile.TemporaryDirectory(prefix="cx-test-", dir="/tmp") as temp:
        path = Path(temp) / "ctl.sock"
        (sw.backup_dir / "live-session.json").write_text(json.dumps({"socket": str(path)}))
        server = await asyncio.start_unix_server(bridge.control, path=path)
        async with server:
            assert await asyncio.to_thread(live_session.switch, sw, "personal") == "Switched"


def test_live_lease_blocks_purge_and_closed_profile_is_removed(live_fleet, monkeypatch):
    from codex_swap.exceptions import LockError

    sw, first, _ = live_fleet
    sw.interactive = True
    monkeypatch.setattr("builtins.input", lambda _question: "y")
    profile = sw.backup_dir / "live-home"
    profile.mkdir()
    (profile / "history.jsonl").write_text("synthetic history")
    with sw.live_session_lock():
        with pytest.raises(LockError):
            sw.purge()
        assert profile.exists()
    sw.purge()
    assert not profile.exists()
    assert read_auth(sw.live_path) == first
