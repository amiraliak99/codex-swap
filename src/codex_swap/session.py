"""Run Codex with the authoritative account home under an owner lease."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from codex_swap import app_server
from codex_swap.credentials import read_auth
from codex_swap.exceptions import SessionError
from codex_swap.fsutil import (
    atomic_write_bytes, is_link, link_points_at, make_dir_link, make_file_link, remove_link,
)
from codex_swap.settings import load_run_settings


def _share(source: Path, dest: Path, private: Path, enabled: bool, *, directory: bool) -> None:
    if is_link(dest):
        if enabled and link_points_at(dest, source):
            return
        if not link_points_at(dest, source):
            raise SessionError(f"Unexpected link at {dest.name} in this account's Codex home.")
        remove_link(dest)
    if not enabled:
        if private.exists() and not dest.exists():
            private.rename(dest)
        return
    if dest.exists():
        if private.exists():
            raise SessionError(f"Resolve the two private copies of {dest.name} before sharing it.")
        private.parent.mkdir(mode=0o700, exist_ok=True)
        dest.rename(private)
    try:
        if directory:
            source.mkdir(parents=True, exist_ok=True)
            make_dir_link(source, dest)
        else:
            source.touch(exist_ok=True)
            make_file_link(source, dest)
    except OSError:
        if private.exists():
            private.rename(dest)
        if directory or os.name != "nt":
            raise SessionError(f"Could not share {dest.name}; use the corresponding no-share flag.") from None


class SessionManager:
    def __init__(self, switcher):
        self.switcher = switcher

    def _launch(self, tail, home, isolated=False) -> None:
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        program = app_server.executable()
        args = [program]
        env = app_server.profile_env(home)
        if isolated:
            args.extend(("-c", 'cli_auth_credentials_store="file"'))
            env["CODEX_SWAP_SESSION"] = "1"
        try:
            code = subprocess.call([*args, *tail], env=env)
        except OSError:
            raise SessionError("Could not start Codex.") from None
        if code:
            raise SystemExit(code if code >= 0 else 128 - code)

    def exec_default(self, tail) -> None:
        if os.environ.get("CODEX_SWAP_SESSION"):
            self._launch(tail, self.switcher.home)
            return
        self._run(None, tail, share=False, share_history=False, require_session=False)

    def run(self, identifier, tail, *, share=True, share_history=None, require_session=False) -> None:
        self.switcher._guard_default()
        if share_history is None:
            share_history = load_run_settings(self.switcher.backup_dir).share_history
        self._run(identifier, tail, share=share, share_history=share_history, require_session=require_session)

    def _run(self, identifier, tail, *, share, share_history, require_session) -> None:
        sw = self.switcher
        lease = None
        try:
            with sw.lock():
                data = sw._get_sequence_data()
                _, live = sw._read_live()
                row = None if identifier is None else data["accounts"][sw._resolve(data, identifier)]
                isolated = row is not None and (live is None or live.key != row["identity"])
                if require_session and not isolated:
                    raise SessionError("This account is already the default Codex login.")
                home = sw.account_home(row["identity"]) if isolated else sw.home
                key = row["identity"] if row else live.key if live else None
                if key is not None:
                    owner = sw.lease(key)
                    if not owner.acquire():
                        raise SessionError("This account already has an active session or refresh.")
                    lease = owner
                if isolated:
                    sw._profile_auth(row)
                    self._prepare(home, share=share, share_history=share_history)
            self._launch(tail, home, isolated)
        finally:
            if lease is not None:
                lease.release()

    def _prepare(self, home: Path, *, share: bool, share_history: bool) -> None:
        source = self.switcher.home
        private = home / ".private"
        for name in ("skills", "rules", "prompts"):
            _share(source / name, home / name, private / name,
                   share and (source / name).is_dir(), directory=True)
        for name in ("AGENTS.md", "AGENTS.override.md"):
            _share(source / name, home / name, private / name,
                   share and (source / name).is_file(), directory=False)
        for name in ("sessions", "archived_sessions"):
            _share(source / name, home / name, private / name, share_history, directory=True)
        for name in ("history.jsonl", "session_index.jsonl"):
            _share(source / name, home / name, private / name, share_history, directory=False)

        # Use an independent config so changes from inside a session cannot
        # rewrite the user's default profile. Local SQLite state is per home.
        config = home / "config.toml"
        mirrored = home / ".mirrored-config"
        if share and (source / "config.toml").is_file():
            if not mirrored.exists() and config.exists():
                private.mkdir(mode=0o700, exist_ok=True)
                if not (private / "config.toml").exists():
                    shutil.copy2(config, private / "config.toml")
            atomic_write_bytes(mirrored, b"")
            atomic_write_bytes(config, (source / "config.toml").read_bytes())
        elif mirrored.exists():
            saved = private / "config.toml"
            if saved.exists():
                saved.replace(config)
            else:
                config.unlink(missing_ok=True)
            mirrored.unlink()


def login(switcher, *, slot=None, alias=None, device_auth=False) -> None:
    """Use Codex's own browser or device login in an empty scratch home."""
    switcher._guard_default()
    with tempfile.TemporaryDirectory(prefix="codexswap-login-") as temp:
        home = Path(temp)
        args = [app_server.executable(), "login", "-c", 'cli_auth_credentials_store="file"']
        if device_auth:
            args.append("--device-auth")
        if subprocess.call(args, cwd=home, env=app_server.profile_env(home)):
            raise SessionError("Codex login did not complete.")
        auth = read_auth(home / "auth.json")
        if auth is None:
            raise SessionError("Codex did not produce a file-backed login.")
        num = switcher.register_auth(auth, slot=slot, alias=alias, prefer_live=True)
        print(f"Saved account {num} ({switcher.account_email(num)}).")
