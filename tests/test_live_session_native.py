"""Exercise consecutive turns through the installed Codex and a fake backend."""

import asyncio
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from codex_swap import app_server, live_session
from codex_swap.credentials import read_auth, write_auth
from codex_swap.switcher import CodexAccountSwitcher
from tests.conftest import login_record


@pytest.fixture
def backend():
    workspaces = [f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2)]
    request_tokens = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, content_type="application/json"):
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path.startswith("/backend-api/wham/accounts/check"):
                self.reply(json.dumps({"accounts": [
                    {"id": workspace, "workspace_backend_origin": "https://chatgpt.com",
                     "account_routing_override": "NO_CONSTRAINT", "can_access_with_session": True}
                    for workspace in workspaces
                ]}))
            elif self.path.startswith("/backend-api/wham/settings/user"):
                self.reply('{"commit_attribution_enabled":false}')
            elif self.path.startswith("/v1/models"):
                self.reply('{"models":[],"data":[]}')
            else:
                self.reply("{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if not self.path.endswith("/responses"):
                self.reply("{}")
                return
            request_tokens.append(self.headers.get("Authorization"))
            ident = f"response-{len(request_tokens)}"
            events = [
                {"type": "response.created", "response": {"id": ident}},
                {"type": "response.output_item.done", "item": {
                    "type": "message", "role": "assistant", "id": ident,
                    "content": [{"type": "output_text", "text": "Synthetic reply"}],
                }},
                {"type": "response.completed", "response": {"id": ident, "usage": {
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                }}},
            ]
            self.reply("".join(f"data: {json.dumps(event)}\n\n" for event in events), "text/event-stream")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", workspaces, request_tokens
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.skipif(os.name == "nt" or not shutil.which("codex"), reason="Requires Codex and Unix sockets")
@pytest.mark.asyncio
async def test_native_store_switches_turns_on_the_same_thread(tmp_path, monkeypatch, backend):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base_url, workspaces, request_tokens = backend
    sw = CodexAccountSwitcher()
    accounts = [login_record(f"user{n}", workspace=workspace) for n, workspace in enumerate(workspaces)]
    write_auth(sw.live_path, accounts[0])
    sw.add_account(alias="work")
    sw.register_auth(accounts[1], alias="personal")
    home = tmp_path / "managed"
    home.mkdir()
    (home / "config.toml").write_text(
        f'chatgpt_base_url = "{base_url}/backend-api"\n'
        'model_provider = "routing"\nmodel = "gpt-5.4"\n'
        '[model_providers.routing]\nname = "OpenAI"\nrequires_openai_auth = true\n'
        f'base_url = "{base_url}/v1"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
        '[analytics]\nenabled = false\n',
        encoding="utf-8",
    )
    grant = live_session._acquire_grant(sw, "work")
    process = await asyncio.create_subprocess_exec(
        app_server.executable(), "app-server", "--stdio", "-c", 'cli_auth_credentials_store="ephemeral"',
        cwd=home, env=app_server.profile_env(home), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=live_session.RPC_LIMIT,
    )
    bridge = live_session._Bridge(sw, grant, process, grant.tokens())
    pump = asyncio.create_task(bridge.read_backend())
    try:
        with tempfile.TemporaryDirectory(prefix="cx-test-", dir="/tmp") as temp:
            ui = Path(temp) / "ui.sock"
            control = Path(temp) / "control.sock"
            (sw.backup_dir / "live-session.json").write_text(json.dumps({"socket": str(control)}))
            control_server = await asyncio.start_unix_server(bridge.control, path=control)
            async with control_server, unix_serve(bridge.connect_frontend, str(ui), compression=None):
                async with unix_connect(str(ui)) as frontend, asyncio.timeout(35):
                    request_id = 0

                    async def receive(predicate):
                        while True:
                            message = json.loads(await frontend.recv())
                            if predicate(message):
                                return message

                    async def call(method, params):
                        nonlocal request_id
                        request_id += 1
                        await frontend.send(json.dumps({"id": request_id, "method": method, "params": params}))
                        reply = await receive(lambda m: m.get("id") == request_id and "method" not in m)
                        assert "result" in reply, f"Codex rejected {method}"
                        return reply["result"]

                    await call("initialize", {"clientInfo": {"name": "codexswap-test", "version": "0.1.0"}})
                    await frontend.send('{"method":"initialized"}')
                    result = await call("thread/start", {"cwd": str(home), "model": "gpt-5.4"})
                    thread_id = result["thread"]["id"]
                    for n in range(2):
                        if n:
                            assert "user1@example.test" in await asyncio.to_thread(live_session.switch, sw, "personal")
                        await call("turn/start", {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": f"Turn {n}", "text_elements": []}],
                        })
                        event = await receive(lambda m: m.get("method") == "turn/completed")
                        assert event["params"]["threadId"] == thread_id
                        assert event["params"]["turn"]["status"] == "completed"
                    assert request_tokens == [f"Bearer {auth['tokens']['access_token']}" for auth in accounts]
                    assert read_auth(sw.live_path) == accounts[0]
    finally:
        await live_session._stop(process)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        bridge.current.lease.release()
