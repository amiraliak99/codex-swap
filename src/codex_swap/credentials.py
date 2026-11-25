"""File-backed auth and identity for Codex CLI rust-v0.172.0.

The native commit sidecar is auth.json.lock at the resolved auth target.
JWT claims are read from credentials the user already owns for local slot
attribution; the tool never treats them as server-verified authorization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codex_swap.exceptions import CredentialReadError
from codex_swap.fsutil import atomic_write_text, read_text_with_retry
from codex_swap.locking import FileLock

AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def claims(token: object) -> dict:
    if not isinstance(token, str):
        return {}
    try:
        payload = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return value if isinstance(value, dict) else {}
    except (IndexError, ValueError, UnicodeError):
        return {}


def field(value: object, key: str) -> str:
    if isinstance(value, dict):
        text = value.get(key)
        if isinstance(text, str) and text and text.isprintable():
            return text
    return ""


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    key: str
    email: str
    workspace: str = ""
    plan: str = ""
    kind: str = "oauth"


def identify(auth: dict) -> Identity:
    mode = auth.get("auth_mode")
    key = field(auth, "OPENAI_API_KEY")
    if key and mode in (None, "apikey"):
        digest = fingerprint(key)
        return Identity(digest, f"api-key-{digest[:12]}@token.local", kind="api_key")

    tokens = auth.get("tokens")
    access_token = field(tokens, "access_token")
    refresh_token = field(tokens, "refresh_token")
    if mode not in (None, "chatgpt") or not access_token or not refresh_token:
        raise CredentialReadError("Expected a managed ChatGPT login or a Codex API-key login.")

    access = claims(access_token)
    identity = claims(field(tokens, "id_token"))
    access_auth = access.get(AUTH_CLAIM)
    id_auth = identity.get(AUTH_CLAIM)
    access_workspace = field(access_auth, "chatgpt_account_id")
    workspace = field(tokens, "account_id") or access_workspace or field(id_auth, "chatgpt_account_id")
    if not workspace or (access_workspace and workspace != access_workspace):
        raise CredentialReadError("The Codex login has a missing or conflicting workspace identity.")
    user = (
        field(access_auth, "chatgpt_user_id")
        or field(access_auth, "user_id")
        or field(id_auth, "chatgpt_user_id")
        or field(identity, "sub")
    )
    email = field(access.get(PROFILE_CLAIM), "email") or field(identity, "email")
    if not user or not email:
        raise CredentialReadError("The Codex login has no user identity or email.")
    plan = field(access_auth, "chatgpt_plan_type") or field(id_auth, "chatgpt_plan_type")
    return Identity(fingerprint(f"chatgpt\0{workspace}\0{user}"), email, workspace, plan)


def read_auth(path: Path) -> dict | None:
    try:
        text = read_text_with_retry(path)
    except FileNotFoundError:
        if path.is_symlink():
            raise CredentialReadError("The Codex auth file points to a missing file.") from None
        return None
    except (OSError, UnicodeError):
        raise CredentialReadError("Could not read the Codex auth file.") from None
    try:
        value = json.loads(text)
    except ValueError:
        raise CredentialReadError("The Codex auth file contains invalid JSON.") from None
    if not isinstance(value, dict):
        raise CredentialReadError("The Codex auth file must contain a JSON object.")
    return value


def write_auth(path: Path, auth: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(auth, indent=2) + "\n")


def check_file_store(home: Path) -> None:
    for override in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        if os.environ.get(override):
            raise CredentialReadError(f"Unset {override} to manage the file-backed Codex login.")
    try:
        with (home / "config.toml").open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        raise CredentialReadError("Could not read Codex config.toml.") from None
    if config.get("cli_auth_credentials_store", "file") != "file":
        raise CredentialReadError("codex-swap requires Codex's cli_auth_credentials_store = \"file\".")


@contextmanager
def auth_lock(path: Path):
    lock_path = path.resolve().with_suffix(".json.lock")
    if lock_path.is_symlink():
        raise CredentialReadError("The Codex credential lock must be a regular file.")
    with FileLock(path.parent / "auth.lock", timeout=5), FileLock(lock_path, timeout=5):
        yield
