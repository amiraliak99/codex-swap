"""Read quota and refresh through Codex CLI's stdio app-server (v0.172.0)."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from codex_swap.credentials import field
from codex_swap.exceptions import CodexSwitchError


class AppServerError(CodexSwitchError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Codex app-server request failed ({reason}).")


def executable() -> str:
    if program := shutil.which("codex"):
        return program
    raise AppServerError("codex-not-found")


def profile_env(home: Path) -> dict[str, str]:
    return {**os.environ, "CODEX_HOME": str(home)}


class AppServer:
    def __init__(
        self, home: Path, *, store: str, cwd: Path, timeout: int = 25,
        experimental_api: bool = False,
    ):
        self.deadline = time.monotonic() + timeout
        self.experimental_api = experimental_api
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.request_id = 0
        try:
            self.process = subprocess.Popen(
                [executable(), "app-server", "--stdio", "-c", f'cli_auth_credentials_store="{store}"',
                 "-c", "analytics.enabled=false"],
                cwd=cwd, env=profile_env(home), text=True, encoding="utf-8",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise AppServerError("could-not-start") from None

    def __enter__(self) -> AppServer:
        def read() -> None:
            try:
                for line in self.process.stdout:
                    self.lines.put(line)
            except (OSError, UnicodeError):
                pass
            finally:
                self.lines.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {
                "clientInfo": {"name": "codex_swap", "version": "0.1.0"},
                "capabilities": {"experimentalApi": self.experimental_api},
            })
            self.send({"method": "initialized"})
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_args) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except OSError:
            raise AppServerError("connection-closed") from None

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("timeout")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise AppServerError("timeout") from None
            if line is None:
                raise AppServerError("connection-closed")
            try:
                message = json.loads(line)
            except ValueError:
                raise AppServerError("bad-response") from None
            if not isinstance(message, dict):
                raise AppServerError("bad-response")
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                continue
            if message.get("id") != self.request_id:
                continue
            if "error" in message:
                detail = field(message["error"], "message")
                status = re.search(r"\b(?:status(?: code)?[ :]+|HTTP\s+)(401|403|429)\b", detail, re.I)
                raise AppServerError(f"http-{status[1]}" if status else "request-rejected")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError("bad-response")
            return result


def read_limits(auth: dict, workspace: str, plan: str) -> dict:
    """Send the access token over stdio; the app-server never gets a refresh token."""
    with tempfile.TemporaryDirectory(prefix="codexswap-usage-") as temp:
        home = Path(temp)
        with AppServer(home, store="ephemeral", cwd=home, experimental_api=True) as server:
            server.request("account/login/start", {
                "type": "chatgptAuthTokens",
                "accessToken": field(auth.get("tokens"), "access_token"),
                "chatgptAccountId": workspace,
                "chatgptPlanType": plan or None,
            })
            result = server.request("account/rateLimits/read", {"excludeResetCreditDetails": True})
    if result.get("accountId") not in (None, workspace):
        raise AppServerError("identity-conflict")
    return normalize_limits(result)


def refresh(home: Path) -> None:
    """Let Codex refresh the credential in its authoritative file-backed profile."""
    with tempfile.TemporaryDirectory(prefix="codexswap-refresh-") as temp:
        with AppServer(home, store="file", cwd=Path(temp), timeout=60) as server:
            server.request("account/read", {"refreshToken": True})


def normalize_limits(result: dict) -> dict:
    """Map native rate-limit windows by duration and keep named weekly buckets."""
    by_id = result.get("rateLimitsByLimitId") or {}
    primary = by_id.get("codex", result.get("rateLimits"))
    if not isinstance(primary, dict):
        raise AppServerError("bad-response")
    usage = {}
    for label in ("primary", "secondary"):
        raw = primary.get(label)
        if isinstance(raw, dict):
            duration = raw.get("windowDurationMins")
            key = {300: "five_hour", 10080: "seven_day"}.get(duration)
            if key:
                usage[key] = _window(raw)
    scoped = []
    for key, bucket in by_id.items():
        if key == "codex" or not isinstance(bucket, dict) or bucket == primary:
            continue
        for raw in (bucket.get("primary"), bucket.get("secondary")):
            if isinstance(raw, dict) and raw.get("windowDurationMins") == 10080:
                name = field(bucket, "normalModelSlug") or field(bucket, "limitName") or key
                scoped.append({"name": name, **_window(raw)})
                break
    if scoped:
        usage["scoped"] = scoped
    if result.get("ordinaryUsageAllowed") is False:
        usage["rejected"] = {"kind": "rate-limit"}
    if not usage:
        raise AppServerError("no-supported-windows")
    return usage


def _window(raw: dict) -> dict:
    pct = raw.get("usedPercent")
    if type(pct) is not int or pct < 0:
        raise AppServerError("bad-response")
    window = {"pct": pct}
    if type(raw.get("resetsAt")) is int:
        try:
            window["resets_at"] = datetime.fromtimestamp(raw["resetsAt"], timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            raise AppServerError("bad-response") from None
    return window
