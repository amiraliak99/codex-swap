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
