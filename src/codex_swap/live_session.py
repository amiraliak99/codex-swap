"""Bridge one Codex TUI and App Server so their thread survives a login change.

Codex CLI 0.172.0 uses WebSockets on Unix sockets for --remote and JSONL on
app-server stdio. The bridge owns external auth RPCs and serializes login
changes with frontend requests. Control commands carry slot names only.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

from codex_swap import app_server, oauth
from codex_swap.credentials import Identity, field, identify, read_auth
from codex_swap.exceptions import CodexSwitchError, SessionError
from codex_swap.fsutil import atomic_write_text
from codex_swap.locking import FileLock

RPC_LIMIT = 64 * 1024 * 1024
SWITCH_TIMEOUT = 95
AUTH_CHANGE_METHODS = {
    "account/logout", "account/login/start", "account/login/cancel",
    "account/bedrock/setup", "account/gcp/setup",
}


@dataclass
class _Grant:
    path: Path
    identity: Identity
    lease: FileLock

    def tokens(self, *, refresh=False) -> dict:
        auth = self._read()
        expires = oauth.access_expiry(auth)
        if refresh or (expires is not None and expires <= time.time() + 30):
            app_server.refresh(self.path.parent)
            successor = self._read()
            if successor == auth:
                raise SessionError("Codex could not renew this account's login.")
            auth = successor
        expires = oauth.access_expiry(auth)
        if expires is not None and expires <= time.time():
            raise SessionError("This account's access token has expired.")
        return {
            "accessToken": field(auth.get("tokens"), "access_token"),
            "chatgptAccountId": self.identity.workspace,
            "chatgptPlanType": identify(auth).plan or None,
        }

    def _read(self) -> dict:
        auth = read_auth(self.path)
        if auth is None or identify(auth).key != self.identity.key:
            raise SessionError("The stored login changed. Re-add the account before switching.")
        return auth


def _acquire_grant(switcher, identifier, current: _Grant | None = None) -> _Grant:
    with switcher.lock():
        data = switcher._get_sequence_data()
        _, live = switcher._read_live()
        if identifier is None:
            identifier = switcher._by_key(data, live.key) if live else None
            if identifier is None:
                raise SessionError("Choose a saved ChatGPT account for the live session.")
        row = data["accounts"][switcher._resolve(data, identifier)]
        if current is not None and row["identity"] == current.identity.key:
            return current
        if row["kind"] != "oauth":
            raise SessionError("Live sessions require a managed ChatGPT login.")
        path = switcher.live_path if live and row["identity"] == live.key else (
            switcher.account_home(row["identity"]) / "auth.json"
        )
        lease = switcher.lease(row["identity"])
        if not lease.acquire():
            raise SessionError("This account is already in use by another codex-swap session or refresh.")
        try:
            auth = read_auth(path)
            if auth is None or (identity := identify(auth)).key != row["identity"]:
                raise SessionError("The saved login is missing or belongs to another account.")
            return _Grant(path, identity, lease)
        except BaseException:
            lease.release()
            raise


def _error(request_id, message: str) -> dict:
    return {"id": request_id, "error": {"code": -32000, "message": message}}


class _Bridge:
    def __init__(self, switcher, current: _Grant, backend, initial_tokens=None):
        self.switcher = switcher
        self.current = current
        self.backend = backend
        self.initial_tokens = initial_tokens
        self.frontend = None
        self.ready = False
        self.switching = False
        self.gate = asyncio.Lock()
        self.pending = {}
        self.internal = {}
        self.turns = set()
        self.active_threads = set()
        self.pending_tools = set()
        self.completed = {}
        self.refresh_task = None
        self.failed = asyncio.Event()

    async def _send(self, message):
        self.backend.stdin.write((json.dumps(message) + "\n").encode())
        await self.backend.stdin.drain()

    async def _request(self, method, params):
        request_id = f"codex-swap:{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.internal[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout=20)
        except (TimeoutError, OSError):
            self.failed.set()
            raise SessionError("The App Server stopped responding. The managed session will close.") from None
        finally:
            del self.internal[request_id]
        if "error" in response:
            raise SessionError("Codex rejected the live authentication request.")
        return response["result"]

    async def _tokens(self, grant, *, refresh=False):
        # Keep the lease until the native refresh process has persisted its
        # successor, including when teardown cancels the waiter.
        worker = asyncio.create_task(asyncio.to_thread(grant.tokens, refresh=refresh))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            raise

    async def _login(self, tokens):
        result = await self._request("account/login/start", {"type": "chatgptAuthTokens", **tokens})
        if result.get("type") != "chatgptAuthTokens":
            self.failed.set()
            raise SessionError("The App Server did not confirm the new login.")

    async def _refresh(self, message):
        request_id = message["id"]
        params = message.get("params") or {}
        try:
            if params.get("previousAccountId") not in (None, self.current.identity.workspace):
                raise SessionError("The refresh request belongs to a different account.")
            result = await self._tokens(self.current, refresh=True)
        except (CodexSwitchError, OSError):
            response = _error(request_id, "Could not renew the managed ChatGPT login.")
        else:
            response = {"id": request_id, "result": result}
        try:
            await self._send(response)
        except OSError:
            self.failed.set()

    def _record_turn(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if method == "thread/status/changed":
            thread = params["threadId"]
            if params["status"]["type"] == "active":
                self.active_threads.add(thread)
            else:
                self.active_threads.discard(thread)
        elif method == "serverRequest/resolved":
            self.pending_tools.discard(params["requestId"])
        elif method == "turn/started":
            self.turns.add(params["turn"]["id"])
        elif method == "turn/completed":
            turn = params["turn"]["id"]
            self.turns.discard(turn)
            # A quick turn can finish before its start RPC settles.
            for request_id, pending_method in self.pending.items():
                if pending_method in ("turn/start", "review/start"):
                    self.completed.setdefault(request_id, set()).add(turn)
        elif "id" in message and method is None:
            request_id = message["id"]
            pending_method = self.pending.pop(request_id, None)
            finished = self.completed.pop(request_id, ())
            if pending_method in ("turn/start", "review/start"):
                turn = (message.get("result") or {}).get("turn") or {}
                if turn.get("status") == "inProgress" and turn["id"] not in finished:
                    self.turns.add(turn["id"])

    async def read_backend(self):
        try:
            async for line in self.backend.stdout:
                message = json.loads(line)
                request_id = message.get("id")
                method = message.get("method")
                if method is None and request_id in self.internal:
                    future = self.internal[request_id]
                    if not future.done():
                        future.set_result(message)
                    continue
                if method == "account/chatgptAuthTokens/refresh":
                    if self.switching or (self.refresh_task is not None and not self.refresh_task.done()):
                        await self._send(_error(request_id, "An authentication refresh is in progress."))
                    else:
                        self.refresh_task = asyncio.create_task(self._refresh(message))
                    continue
                if method and request_id is not None:
                    if not self.ready:
                        await self._send(_error(request_id, "The managed frontend is starting."))
                        continue
                    self.pending_tools.add(request_id)
                self._record_turn(message)
                if self.ready:
                    await self.frontend.send(line.decode().rstrip("\n"))
        except (ConnectionClosed, ValueError, OSError):
            pass
        finally:
            self.failed.set()

    async def connect_frontend(self, frontend):
        if self.frontend is not None:
            await frontend.close(code=1008, reason="A Codex frontend already owns this session")
            return
        self.frontend = frontend
        try:
            async with asyncio.timeout(10):
                initial = json.loads(await frontend.recv())
            if initial.get("method") != "initialize":
                raise SessionError("Codex did not initialize the live frontend.")
            params = initial["params"]
            params.setdefault("capabilities", {})["experimentalApi"] = True
            result = await self._request("initialize", params)
            await self._send({"method": "initialized"})
            await self._login(self.initial_tokens)
            self.initial_tokens = None
            await frontend.send(json.dumps({"id": initial["id"], "result": result}))
            self.ready = True
            async for raw in frontend:
                message = json.loads(raw)
                method = message.get("method")
                if method == "initialized":
                    continue
                async with self.gate:
                    if self.failed.is_set():
                        break
                    request_id = message.get("id")
                    if request_id is not None:
                        if method in AUTH_CHANGE_METHODS:
                            await frontend.send(json.dumps(_error(
                                request_id, "Use codexswap live-switch to change this session's account."
                            )))
                            continue
                        if method is None:
                            self.pending_tools.discard(request_id)
                        else:
                            self.pending[request_id] = method
                    await self._send(message)
        except SessionError as exc:
            if not self.ready and "initial" in locals() and "id" in initial:
                await frontend.send(json.dumps(_error(initial["id"], str(exc))))
            self.failed.set()
        except (ConnectionClosed, TimeoutError, OSError, ValueError):
            pass
        finally:
            self.ready = False

    def _require_idle(self):
        refreshing = self.refresh_task is not None and not self.refresh_task.done()
        if not self.ready or self.failed.is_set():
            raise SessionError("The live Codex frontend is not ready.")
        if self.pending or self.turns or self.active_threads or self.pending_tools or refreshing:
            raise SessionError("Codex is busy. Wait until the current turn and requests finish.")

    async def _switch(self, identifier):
        async with self.gate:
            self._require_idle()
            grant = _acquire_grant(self.switcher, identifier, self.current)
            if grant is self.current:
                return f"Already using {grant.identity.email}."
            self.switching = True
            try:
                tokens = await self._tokens(grant)
                self._require_idle()
                await self._login(tokens)
            except BaseException:
                grant.lease.release()
                raise
            finally:
                self.switching = False
            self.current.lease.release()
            self.current = grant
            return f"Live session switched to {grant.identity.email}."

    async def control(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(raw)
            identifier = request.get("account") if isinstance(request, dict) else None
            if not isinstance(identifier, str) or not identifier.strip():
                raise SessionError("Choose an account slot, email or alias.")
            reply = {"message": await self._switch(identifier)}
        except CodexSwitchError as exc:
            reply = {"error": str(exc)}
        except (OSError, ValueError, TimeoutError):
            reply = {"error": "Invalid live switch request."}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()


async def _stop(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _serve(switcher, grant, home, temp, tail, initial_tokens):
    program = app_server.executable()
    env = app_server.profile_env(home)
    env["CODEX_SWAP_SESSION"] = "1"
    backend = await asyncio.create_subprocess_exec(
        program, "app-server", "--stdio", "-c", 'cli_auth_credentials_store="ephemeral"',
        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=RPC_LIMIT, start_new_session=True,
    )
    bridge = _Bridge(switcher, grant, backend, initial_tokens)
    read_task = asyncio.create_task(bridge.read_backend())
    failure = asyncio.create_task(bridge.failed.wait())
    tui = None
    registry = switcher.backup_dir / "live-session.json"
    ui_path, control_path = temp / "ui.sock", temp / "control.sock"
    try:
        async with unix_serve(
            bridge.connect_frontend, str(ui_path), compression=None, max_size=RPC_LIMIT,
        ):
            control = await asyncio.start_unix_server(bridge.control, path=control_path, limit=4096)
            async with control:
                atomic_write_text(registry, json.dumps({"socket": str(control_path)}), follow_symlinks=False)
                tui = await asyncio.create_subprocess_exec(
                    program, "--remote", f"unix://{ui_path}", *tail, env=env
                )
                wait_tui = asyncio.create_task(tui.wait())
                await asyncio.wait((wait_tui, failure), return_when=asyncio.FIRST_COMPLETED)
                if failure.done() and not wait_tui.done():
                    raise SessionError("The managed App Server connection closed.")
                return wait_tui.result()
    finally:
        registry.unlink(missing_ok=True)
        if tui is not None:
            await _stop(tui)
        # Wait for accepted switch commands and refreshes before releasing a grant.
        async with bridge.gate:
            await _stop(backend)
            if bridge.refresh_task is not None:
                await asyncio.gather(bridge.refresh_task, return_exceptions=True)
            bridge.current.lease.release()
        failure.cancel()
        read_task.cancel()
        await asyncio.gather(read_task, failure, return_exceptions=True)


def run(manager, identifier, tail, *, share, share_history):
    if os.name == "nt":
        raise SessionError("Experimental live sessions require a Unix socket on Linux or macOS.")
    if any(arg.startswith("--remote") for arg in tail):
        raise SessionError("The live session manages its own Codex --remote endpoint.")
    sw = manager.switcher
    sw._guard_default()
    with sw.live_session_lock():
        grant = _acquire_grant(sw, identifier)
        try:
            home = sw.backup_dir / "live-home"
            home.mkdir(mode=0o700, exist_ok=True)
            manager._prepare(home, share=share, share_history=share_history)
            initial_tokens = grant.tokens()
            with tempfile.TemporaryDirectory(prefix="cxsw-", dir="/tmp") as temp:
                code = asyncio.run(_serve(sw, grant, home, Path(temp), tail, initial_tokens))
        finally:
            grant.lease.release()
    if code:
        raise SystemExit(code if code > 0 else 128 - code)


def switch(switcher, identifier: str) -> str:
    try:
        registry = switcher.backup_dir / "live-session.json"
        path = json.loads(registry.read_text(encoding="utf-8"))["socket"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(SWITCH_TIMEOUT)
            client.connect(path)
            client.sendall((json.dumps({"account": identifier}) + "\n").encode())
            with client.makefile("rb") as reader:
                reply = json.loads(reader.readline(4096))
    except (OSError, ValueError, KeyError):
        raise SessionError("Could not reach the live Codex session. Start one with codexswap run --live.") from None
    if isinstance(reply, dict):
        if isinstance(reply.get("error"), str):
            raise SessionError(reply["error"])
        if isinstance(reply.get("message"), str):
            return reply["message"]
    raise SessionError("The live Codex session returned an invalid reply.")
