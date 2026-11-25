"""Credential fingerprints and quota presentation shared by Codex Swap surfaces."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from codex_swap.credentials import claims, field, fingerprint

FIVE_HOUR_LABEL = "5h"
REJECTED_LABEL = "rejected"
USAGE_SOURCE_HEADERS = "headers"
LOGIN_DUE_WITHIN_S = 7 * 86400


def credential_fingerprint(credentials: str | None) -> str | None:
    if not credentials:
        return None
    try:
        token = field(json.loads(credentials).get("tokens"), "refresh_token")
    except (AttributeError, ValueError):
        return None
    return fingerprint(token) if token else None


def access_expiry(auth: dict) -> int | None:
    expires = claims(field(auth.get("tokens"), "access_token")).get("exp")
    return expires if type(expires) is int else None

def login_time_left(
    expires_at: float | None, quarantined: bool, now: float | None = None
) -> float | None:
    if quarantined or expires_at is None:
        return None
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    remaining = expires_at - now
    if remaining > LOGIN_DUE_WITHIN_S:
        return None
    return max(0.0, remaining)


def format_login_expiry(
    expires_at: float | None,
    quarantined: bool,
    now: float | None = None,
    short: bool = False,
) -> str | None:
    left = login_time_left(expires_at, quarantined, now)
    if left is None:
        return None
    if left <= 0:
        return "re-login needed"
    countdown = format_countdown(left)
    return f"re-login in {countdown.split(' ')[0] if short else countdown}"


def login_expiry_clock(expires_at: float, now: float | None = None) -> str:
    now_utc = (
        datetime.fromtimestamp(now, tz=timezone.utc) if now is not None
        else datetime.now(timezone.utc)
    )
    return reset_clock_string(datetime.fromtimestamp(expires_at, tz=timezone.utc), now_utc)


def format_reset(resets_at: str) -> tuple[str, str]:
    reset_utc = datetime.fromisoformat(resets_at)
    now = datetime.now(timezone.utc)
    return format_countdown((reset_utc - now).total_seconds()), reset_clock_string(reset_utc, now)


def format_countdown(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def reset_clock_string(reset_utc: datetime, now_utc: datetime) -> str:
    reset_local = reset_utc.astimezone()
    now_local = now_utc.astimezone()
    if reset_local.date() == now_local.date():
        return reset_local.strftime("%H:%M")
    day = str(reset_local.day)
    return reset_local.strftime(f"%b {day} %H:%M")


def fresh_reset_strings(window: dict) -> tuple[str, str] | None:
    resets_at = window.get("resets_at")
    if resets_at:
        try:
            return format_reset(resets_at)
        except (ValueError, TypeError):
            pass  # unparseable cached value — fall back below
    if "clock" in window:
        return window.get("countdown", "?"), window["clock"]
    return None


def _header_reading_complete(usage: dict, models: Sequence[str]) -> bool:
    if not isinstance(usage.get("seven_day"), dict):
        return False
    if not models:
        return True
    wanted = {m.lower() for m in models}
    if "all" in wanted:
        return False
    have = {
        s["name"].lower()
        for s in usage.get("scoped") or []
        if isinstance(s, dict) and isinstance(s.get("name"), str)
    }
    return wanted <= have


def relevant_windows(
    usage: dict | None, models: Sequence[str] = ()
) -> list[tuple[str, float, str | None]]:
    if not isinstance(usage, dict):
        return []
    windows: list[tuple[str, float, str | None]] = []
    if usage.get("source") != USAGE_SOURCE_HEADERS or _header_reading_complete(
        usage, models
    ):
        for key, label in (("five_hour", FIVE_HOUR_LABEL), ("seven_day", "7d")):
            window = usage.get(key)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
                windows.append((label, float(window["pct"]), window.get("resets_at")))
        if models:
            wanted = {m.lower() for m in models}
            match_all = "all" in wanted
            scoped = usage.get("scoped")
            if isinstance(scoped, list):
                for s in scoped:
                    if (
                        isinstance(s, dict)
                        and isinstance(s.get("pct"), (int, float))
                        and isinstance(s.get("name"), str)
                        and (match_all or s["name"].lower() in wanted)
                    ):
                        windows.append((s["name"], float(s["pct"]), s.get("resets_at")))
    rejected = usage.get("rejected")
    if isinstance(rejected, dict):
        windows.append((REJECTED_LABEL, 100.0, rejected.get("resets_at")))
    return windows


def account_headroom(
    usage: dict | None, models: Sequence[str] = ()
) -> float | None:
    pcts = [pct for _, pct, _ in relevant_windows(usage, models)]
    if not pcts:
        return None
    return 100.0 - max(pcts)
