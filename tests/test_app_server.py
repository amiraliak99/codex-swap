import shutil

import pytest

from codex_swap.app_server import AppServer, AppServerError, normalize_limits


def test_native_limits_select_by_duration_and_model_bucket():
    def window(minutes, percent):
        return {"usedPercent": percent, "windowDurationMins": minutes, "resetsAt": 1_800_000_000}

    core = {"primary": window(10080, 55), "secondary": window(300, 25)}
    usage = normalize_limits({
        "rateLimits": core,
        "rateLimitsByLimitId": {"codex": core, "other": {"primary": window(10080, 72)}},
        "ordinaryUsageAllowed": False,
    })
    assert usage["five_hour"]["pct"] == 25
    assert usage["seven_day"]["pct"] == 55
    assert usage["scoped"][0]["name"] == "other"
    assert usage["scoped"][0]["pct"] == 72
    assert "rejected" in usage


def test_native_limits_without_supported_duration_raise():
    with pytest.raises(AppServerError, match="no-supported-windows"):
        normalize_limits({"rateLimits": {"primary": {"usedPercent": 8}}})


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_real_app_server_initialization_with_empty_home(tmp_path):
    home = tmp_path / "scratch"
    home.mkdir()
    with AppServer(home, store="ephemeral", cwd=home) as server:
        response = server.request("account/read", {"refreshToken": False})
    assert response["account"] is None


def test_quota_reader_opts_in_to_external_auth(monkeypatch):
    from codex_swap import app_server

    calls = []

    class Server:
        def __init__(self, _home, *, store, cwd, experimental_api):
            assert store == "ephemeral"
            assert experimental_api is True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def request(self, method, params):
            calls.append((method, params))
            return {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 25}}}

    monkeypatch.setattr(app_server, "AppServer", Server)
    assert app_server.read_limits(
        {"tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}},
        "workspace", "plus",
    ) == {"five_hour": {"pct": 25}}
    assert calls == [
        ("account/login/start", {
            "type": "chatgptAuthTokens", "accessToken": "synthetic-access",
            "chatgptAccountId": "workspace", "chatgptPlanType": "plus",
        }),
        ("account/rateLimits/read", {"excludeResetCreditDetails": True}),
    ]
