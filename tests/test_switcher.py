import json
import os
from pathlib import Path

import pytest

from codex_swap import app_server
from codex_swap.autoswitch import AutoSwitchEngine, TickOutcome
from codex_swap.credentials import identify, read_auth, write_auth
from codex_swap.exceptions import CredentialReadError, LockError
from codex_swap.session import SessionManager
from codex_swap.settings import AutoSwitchSettings
from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.transfer import export_accounts, import_accounts
from tests.conftest import login_record


@pytest.fixture
def fleet():
    sw = CodexAccountSwitcher()
    first, second = login_record("first"), login_record("second")
    write_auth(sw.live_path, first)
    sw.add_account(alias="work")
    sw.register_auth(second, alias="personal")
    return sw, first, second


def test_switch_checkpoints_latest_grant(fleet, capsys):
    sw, first, second = fleet
    updated = login_record("first", generation="2")
    write_auth(sw.live_path, updated)
    assert sw.switch_to("personal", json_output=True)["switched"]
    assert read_auth(sw.live_path) == second
    assert sw.current_account_number() == "2"
    assert sw.switch_to("work", json_output=True)["from"]["number"] == 2
    assert read_auth(sw.live_path) == updated
    assert not sw.switch_to("work", json_output=True)["switched"]
    assert updated["tokens"]["refresh_token"] not in capsys.readouterr().out


def test_stale_outgoing_slot_cannot_capture_foreign_login(fleet):
    sw, first, second = fleet
    unknown = login_record("different-user")
    write_auth(sw.live_path, unknown)
    sw.switch_to("2")
    assert list(sw.list_unclaimed_credentials().values()) == [
        {"email": "different-user@example.test", "reason": "unmanaged-login"},
    ]
    assert read_auth(sw.account_home(identify(first).key) / "auth.json") == first


def test_wrong_target_is_rejected_before_touching_live_login(fleet):
    sw, first, second = fleet
    write_auth(sw.account_home(identify(second).key) / "auth.json", first)
    with pytest.raises(CredentialReadError, match="different account"):
        sw.switch_to("2")
    assert read_auth(sw.live_path) == first


def test_live_switch_write_failure_leaves_outgoing_grant_recoverable(fleet, monkeypatch):
    sw, _, _ = fleet
    updated = login_record("first", generation="2")
    write_auth(sw.live_path, updated)

    def fail_live(path, auth):
        if path == sw.live_path:
            raise OSError("simulated write failure")
        write_auth(path, auth)

    monkeypatch.setattr("codex_swap.switcher.write_auth", fail_live)
    with pytest.raises(OSError):
        sw.switch_to("2")
    assert read_auth(sw.live_path) == updated
    assert read_auth(sw.account_home(identify(updated).key) / "auth.json") == updated


def test_move_retains_credentials_and_policies(fleet):
    sw, first, _ = fleet
    sw.set_account_priority("work", "20")
    sw.set_account_threshold("work", "85")
    assert sw.move_account("work", 3) == ("1", "3", False)
    assert sw.current_account_number() == "3"
    assert sw.account_policies()["3"].threshold == 85
    assert sw.account_policies()["3"].priority == 20
    assert json.loads(sw.read_account_credentials("3", "first@example.test")) == first


def test_session_grant_is_authoritative_and_cannot_switch_while_owned(fleet, monkeypatch):
    sw, first, second = fleet
    rotated = login_record("second", generation="2")

    def run(args, env):
        session_path = Path(env["CODEX_HOME"])
        assert session_path / "auth.json" != sw.live_path
        assert read_auth(session_path / "auth.json") == second
        assert args[-2:] == ["resume", "--last"]
        with pytest.raises(LockError):
            sw.switch_to("2")
        write_auth(session_path / "auth.json", rotated)
        return 0

    monkeypatch.setattr(app_server, "executable", lambda: "codex")
    monkeypatch.setattr("codex_swap.session.subprocess.call", run)
    SessionManager(sw).run("2", ["resume", "--last"], share=False, share_history=False)
    assert read_auth(sw.live_path) == first
    sw.switch_to("2")
    assert read_auth(sw.live_path) == rotated


def test_quota_snapshot_and_auto_switch(fleet, monkeypatch):
    sw, _, _ = fleet

    def usage(auth, workspace, plan):
        assert workspace == "workspace" and plan == "plus"
        return {
            "five_hour": {"pct": 99 if identify(auth).email.startswith("first") else 10},
            "seven_day": {"pct": 30},
        }

    monkeypatch.setattr(app_server, "read_limits", usage)
    assert sw.list_accounts(json_output=True)["accounts"][0]["usageStatus"] == "ok"
    engine = AutoSwitchEngine(sw, AutoSwitchSettings(), on_event=lambda event: None)
    assert engine.tick() is TickOutcome.SWITCHED
    assert sw.current_account_number() == "2"


def test_refreshes_only_authoritative_inactive_profile(fleet, monkeypatch):
    sw, first, second = fleet
    retired = login_record("second", expires=1)
    renewed = login_record("second", generation="2")
    target = sw.account_home(identify(second).key)
    write_auth(target / "auth.json", retired)

    def refresh(home):
        assert home == target
        assert read_auth(home / "auth.json") == retired
        write_auth(home / "auth.json", renewed)

    monkeypatch.setattr(app_server, "refresh", refresh)
    assert sw.refresh_account("1", "first@example.test") == "skip-live-login"
    assert sw.refresh_account("2", "second@example.test") == "ok"
    assert read_auth(target / "auth.json") == renewed
    assert read_auth(sw.live_path) == first


def test_keyring_mode_is_rejected_before_live_write(fleet):
    sw, first, _ = fleet
    (sw.home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
    with pytest.raises(CredentialReadError, match="requires"):
        sw.switch_to("2")
    assert read_auth(sw.live_path) == first


def test_credential_export_and_import(fleet, tmp_path, monkeypatch):
    sw, first, second = fleet
    backup = tmp_path / "backup.codexswap"
    export_accounts(sw, str(backup))
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("CODEX_SWAP_HOME", str(tmp_path / "other-store"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-codex"))
    restored = CodexAccountSwitcher()
    import_accounts(restored, str(backup))
    assert restored.current_account_number() is None
    assert json.loads(restored.read_account_credentials("1", "first@example.test")) == first
    assert json.loads(restored.read_account_credentials("2", "second@example.test")) == second


def test_best_does_not_switch_away_from_the_account_with_more_room(fleet, monkeypatch):
    sw, _, _ = fleet

    def usage(auth, _workspace, _plan):
        pct = 10 if identify(auth).email == "first@example.test" else 40
        return {"five_hour": {"pct": pct}}

    monkeypatch.setattr(app_server, "read_limits", usage)
    result = sw.switch(strategy="best", json_output=True)
    assert result["switched"] is False
    assert result["reason"] == "already-best"
    assert sw.current_account_number() == "1"
