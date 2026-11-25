"""Manage account slots and the single authoritative copy of each Codex grant.

The live auth file owns the active grant. Inactive grants live in account homes
keyed by identity, independent of slot number. A switch checkpoints the outgoing
grant before publishing the target; active state is read from auth, not metadata.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from codex_swap import app_server, oauth, paths, poll_policy
from codex_swap.credentials import (
    Identity, auth_lock, check_file_store, field, identify, read_auth, write_auth,
)
from codex_swap.exceptions import (
    AccountNotFoundError, CodexSwitchError, CredentialReadError, SwitchError, ValidationError,
)
from codex_swap.fsutil import atomic_write_text, is_link, read_text_with_retry
from codex_swap.json_output import (
    SCHEMA_VERSION, USAGE_API_KEY, USAGE_RELOGIN_REQUIRED, USAGE_TOKEN_EXPIRED,
    account_ref, account_row,
)
from codex_swap.locking import FileLock
from codex_swap.logging_config import setup_logging
from codex_swap.mappings import MappingStore
from codex_swap.models import (
    AccountPolicy, AccountsSnapshot, AccountSnapshot,
    normalize_account_priority, normalize_account_threshold, normalize_alias,
)
from codex_swap.printer import format_age
from codex_swap.settings import load_settings, parse_model_names
from codex_swap.usage_store import FetchRecord, UsageEntry, UsageStore

SENTINEL_NOTES = {
    USAGE_API_KEY: "API key (no ChatGPT quota)",
    USAGE_TOKEN_EXPIRED: "token expired — open Codex to renew it, or log in again",
    USAGE_RELOGIN_REQUIRED: "re-login needed — run codexswap add --login",
}
ERROR_NOTES = {
    "codex-not-found": "Install Codex CLI and put it on PATH.",
    "http-429": "Codex rate limited the usage read.",
    "request-rejected": "Codex declined the usage read.",
}


def last_seen_note(entry: UsageEntry) -> str | None:
    headroom = oauth.account_headroom(entry.last_good)
    if headroom is None or entry.fetched_at is None:
        return None
    return f"last seen {100 - headroom:.0f}% used · {format_age(int(entry.fetched_at * 1000))}"


class CodexAccountSwitcher:
    def __init__(self, debug: bool = False, interactive: bool = False):
        self.home = paths.get_codex_home()
        self.backup_dir = paths.get_backup_root().resolve()
        self.sequence_file = self.backup_dir / "sequence.json"
        self.live_path = self.home / "auth.json"
        self.interactive = interactive
        self._logger = setup_logging(self.backup_dir, debug)
        self._usage_store = UsageStore(self.backup_dir / "cache")
        self._poll_inputs_override = None
        if self.home.resolve() == self.backup_dir or self.backup_dir in self.home.resolve().parents:
            raise ValidationError("Codex home and the swap store must be separate directories.")
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _is_running_in_container(self) -> bool:
        return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()

    def lock(self) -> FileLock:
        return FileLock(self.backup_dir / ".store.lock")

    def account_home(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValidationError("Invalid stored account identity.")
        return self.backup_dir / "accounts" / key

    def lease(self, key: str) -> FileLock:
        return FileLock(self.account_home(key) / ".owner.lock", timeout=0)

    def _get_sequence_data(self) -> dict:
        try:
            data = json.loads(read_text_with_retry(self.sequence_file))
        except FileNotFoundError:
            return {"accounts": {}}
        except (ValueError, OSError):
            raise ValidationError("Could not read the slot registry.") from None
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise ValidationError("Invalid slot registry.")
        if (bound_home := data.get("codexHome")) is not None and bound_home != str(self.home.resolve()):
            raise ValidationError("This store belongs to another Codex home. Set a separate CODEX_SWAP_HOME.")
        for num, row in data["accounts"].items():
            if not re.fullmatch(r"[1-9][0-9]{0,8}", num) or not field(row, "email"):
                raise ValidationError("Invalid slot registry entry.")
            self.account_home(field(row, "identity"))
        return data

    def _write_slots(self, data: dict) -> None:
        atomic_write_text(self.sequence_file, json.dumps({**data, "codexHome": str(self.home.resolve())}, indent=2) + "\n")

    @staticmethod
    def _find_account_slot(data: dict, email: str, workspace: str) -> str | None:
        matches = [num for num, row in data.get("accounts", {}).items()
                   if row["email"].casefold() == email.casefold()
                   and row.get("organizationUuid", "") == workspace]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _by_key(data: dict, key: str) -> str | None:
        return next((n for n, row in data["accounts"].items() if row["identity"] == key), None)

    def _resolve(self, data: dict, identifier: str) -> str:
        query = str(identifier).strip().casefold()
        if query in data["accounts"]:
            return query
        matches = [n for n, row in data["accounts"].items()
                   if query and query in (row["email"].casefold(), row.get("alias", "").casefold())]
        if len(matches) != 1:
            message = "Multiple accounts match; use a slot number." if matches else "Account not found."
            raise AccountNotFoundError(message)
        return matches[0]

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        data = self._get_sequence_data()
        num = self._resolve(data, identifier)
        row = data["accounts"][num]
        return num, row["email"], row.get("organizationUuid", "")

    def _read_live(self) -> tuple[dict | None, Identity | None]:
        check_file_store(self.home)
        auth = read_auth(self.live_path)
        return auth, identify(auth) if auth is not None else None

    def _get_current_account(self) -> tuple[str, str] | None:
        _, identity = self._read_live()
        return (identity.email, identity.workspace) if identity else None

    def current_account_number(self) -> str | None:
        _, identity = self._read_live()
        return self._by_key(self._get_sequence_data(), identity.key) if identity else None

    def has_live_login(self) -> bool:
        try:
            return self._read_live()[1] is not None
        except CredentialReadError:
            return False

    def account_identity(self, account_num: str) -> dict:
        return self._get_sequence_data()["accounts"].get(str(account_num), {})

    def account_email(self, account_num: str) -> str:
        return self.account_identity(account_num).get("email", "")

    def account_kind_for(self, account_num: str) -> str:
        return self.account_identity(account_num).get("kind", "oauth")

    def _guard_default(self) -> None:
        if os.environ.get("CODEX_SWAP_SESSION"):
            raise SwitchError("Run this command outside the codex-swap session.")

    def _profile_auth(self, row: dict) -> dict:
        auth = read_auth(self.account_home(row["identity"]) / "auth.json")
        if auth is None or identify(auth).key != row["identity"]:
            raise CredentialReadError("Stored Codex credentials are missing or belong to a different account.")
        return auth

    def credential_for(self, row: dict, live: tuple[dict | None, Identity | None]) -> dict:
        auth, identity = live
        if identity and identity.key == row["identity"]:
            return auth
        return self._profile_auth(row)

    def read_account_credentials(self, account_num: str, email: str) -> str | None:
        row = self.account_identity(account_num)
        if not row or row["email"] != email:
            return None
        try:
            return json.dumps(self.credential_for(row, self._read_live()))
        except CredentialReadError:
            return None

    def session_busy(self, key: str) -> bool:
        lease = self.lease(key)
        if not lease.acquire():
            return True
        lease.release()
        return False

    @staticmethod
    def _slot_number(slot: int) -> str:
        if not isinstance(slot, int) or not 1 <= slot <= 999_999_999:
            raise ValidationError("Slot must be between 1 and 999999999.")
        return str(slot)

    @staticmethod
    def _next_slot(rows: dict) -> str:
        return str(next(n for n in range(1, len(rows) + 2) if str(n) not in rows))

    @staticmethod
    def _check_alias(data: dict, alias: str, slot: str) -> None:
        if any(alias == row.get("alias", "").casefold() or alias == row["email"].casefold()
               for n, row in data["accounts"].items() if slot != n):
            raise ValidationError("Alias already belongs to another account.")

    def register_auth(
        self, auth: dict, *, slot: int | None = None, alias: str | None = None,
        email: str | None = None, force: bool = False, prefer_live: bool = False,
    ) -> str:
        identity = identify(auth)
        with self.lock(), auth_lock(self.live_path):
            if prefer_live:
                _, live = self._read_live()
                if live and live.key == identity.key:
                    # When reauthenticating the default account, publish the
                    # fresh grant to the actual owner as well as its checkpoint.
                    with self.lease(identity.key):
                        num = self._register_auth(auth, identity, slot, alias, email, force, owner_held=True)
                        write_auth(self.live_path, auth)
                        return num
            return self._register_auth(auth, identity, slot, alias, email, force)

    def _register_auth(self, auth, identity, slot, alias, email, force, *, owner_held=False) -> str:
        data = self._get_sequence_data()
        rows = data["accounts"]
        existing = self._by_key(data, identity.key)
        num = self._slot_number(slot) if slot is not None else existing or self._next_slot(rows)
        if existing and existing != num:
            raise ValidationError(f"This account already occupies slot {existing}; use move.")
        previous = rows.get(num, {})
        if previous and previous["identity"] != identity.key:
            if not force and not (self.interactive and input(f"Replace account {num}? [y/N] ").strip().lower() == "y"):
                raise ValidationError("Slot is occupied. Use move or choose an empty slot.")
            if self.session_busy(previous["identity"]):
                raise ValidationError("Exit the account's session before replacing its slot.")
        if alias is not None:
            alias = normalize_alias(alias)
            self._check_alias(data, alias, num)
        if email is not None and (identity.kind != "api_key" or not re.fullmatch(r"[^@\s]+@[^@\s]+", email)):
            raise ValidationError("Provide an email label for an API-key account.")
        row = {
            **{k: previous[k] for k in ("disabled", "threshold", "priority", "alias") if k in previous},
            "identity": identity.key, "email": email or (
                previous.get("email", identity.email) if identity.kind == "api_key" else identity.email
            ), "organizationUuid": identity.workspace, "kind": identity.kind,
            "plan": identity.plan, "updatedAt": time.time(),
        }
        if alias is not None:
            row["alias"] = alias
        owner = contextlib.nullcontext() if owner_held else self.lease(identity.key)
        with owner:
            if previous and previous["identity"] != identity.key:
                old_auth = self._profile_auth(previous)
                self._stash(old_auth, previous["email"], "slot-replaced")
            write_auth(self.account_home(identity.key) / "auth.json", auth)
            rows[num] = row
            self._write_slots(data)
        return num

    def add_account(self, slot: int | None = None, alias: str | None = None) -> None:
        self._guard_default()
        with self.lock(), auth_lock(self.live_path):
            auth, identity = self._read_live()
            if identity is None:
                raise CredentialReadError("Log into Codex first with codex login.")
            num = self._register_auth(auth, identity, slot, alias, None, False)
        self._usage_store.clear_dead_token([num], {num: self._cache_identity(self.account_identity(num))})
        print(f"Saved account {num} ({self.account_email(num)}).")

    def add_account_from_token(self, token="", email=None, slot=None) -> None:
        import getpass
        import sys

        if token == "-":
            token = sys.stdin.read().strip()
        elif not token:
            token = getpass.getpass("OpenAI API key: ").strip()
        if not re.fullmatch(r"sk-[A-Za-z0-9_-]+", token):
            raise ValidationError("Expected an OpenAI API key.")
        num = self.register_auth({"auth_mode": "apikey", "OPENAI_API_KEY": token}, slot=slot, email=email)
        print(f"Saved API-key account {num}.")

    def _stash(self, auth: dict, email: str, reason: str) -> None:
        entry = {"email": email, "reason": reason, "auth": auth}
        directory = self.backup_dir / "unclaimed"
        directory.mkdir(mode=0o700, exist_ok=True)
        atomic_write_text(directory / f"{uuid.uuid4().hex}.json", json.dumps(entry))

    def list_unclaimed_credentials(self) -> dict[str, dict]:
        result = {}
        for path in sorted((self.backup_dir / "unclaimed").glob("*.json")):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            result[path.stem] = {k: doc[k] for k in ("email", "reason")}
        return result

    def remove_unclaimed_credential(self, entry: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", entry):
            raise ValidationError("Invalid unclaimed entry ID.")
        with self.lock():
            (self.backup_dir / "unclaimed" / f"{entry}.json").unlink(missing_ok=True)

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        with self.lock():
            data = self._get_sequence_data()
            num = self._resolve(data, identifier)
            row = data["accounts"][num]
            if not assume_yes and self.interactive and input(f"Remove account {num}? [y/N] ").lower() != "y":
                return
            with self.lease(row["identity"]):
                del data["accounts"][num]
                self._write_slots(data)
                (self.account_home(row["identity"]) / "auth.json").unlink(missing_ok=True)
            MappingStore(self.backup_dir).prune_account(row["email"], row.get("organizationUuid", ""))
        print(f"Removed account {num}.")

    def move_account(self, identifier: str, slot: int) -> tuple[str, str, bool]:
        return self._relocate(identifier, self._slot_number(slot), require_existing=False)

    def swap_accounts(self, first: str, second: str) -> tuple[str, str]:
        a, b, _ = self._relocate(first, second, require_existing=True)
        return a, b

    def _relocate(self, first, second, *, require_existing):
        with self.lock():
            data = self._get_sequence_data()
            rows = data["accounts"]
            a = self._resolve(data, first)
            b = self._resolve(data, second) if require_existing else second
            swapped = b in rows and a != b
            if swapped:
                rows[a], rows[b] = rows[b], rows[a]
            elif a != b:
                rows[b] = rows.pop(a)
            self._write_slots(data)
        return a, b, swapped

    def list_aliases(self) -> list[tuple[str, str, str]]:
        return [(n, row["alias"], row["email"])
                for n, row in sorted(self._get_sequence_data()["accounts"].items(), key=lambda item: int(item[0]))
                if row.get("alias")]

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        alias = normalize_alias(alias)
        return self._set_field(identifier, "alias", alias), alias

    def unset_alias(self, identifier: str) -> str:
        return self._set_field(identifier, "alias", None)

    def _set_field(self, identifier, key, value) -> str:
        with self.lock():
            data = self._get_sequence_data()
            num = self._resolve(data, identifier)
            row = data["accounts"][num]
            if key == "alias" and value:
                self._check_alias(data, value, num)
            if value is None or value is False:
                row.pop(key, None)
            else:
                row[key] = value
            self._write_slots(data)
        return num

    @staticmethod
    def _policy(row: dict) -> AccountPolicy:
        return AccountPolicy(threshold=row.get("threshold"), priority=row.get("priority", 0))

    def set_account_threshold(self, identifier: str, threshold) -> None:
        self._set_field(identifier, "threshold", None if threshold is None else normalize_account_threshold(threshold))

    def set_account_priority(self, identifier: str, priority) -> None:
        self._set_field(identifier, "priority", None if priority is None else normalize_account_priority(priority))

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self._set_field(identifier, "disabled", disabled)

    def account_policies(self) -> dict[str, AccountPolicy]:
        return {n: self._policy(row) for n, row in self._get_sequence_data()["accounts"].items()}

    def disabled_account_numbers(self) -> list[str]:
        return [n for n, row in self._get_sequence_data()["accounts"].items() if row.get("disabled")]

    def switchable_account_numbers(self) -> list[str]:
        return sorted((n for n, row in self._get_sequence_data()["accounts"].items()
                       if not row.get("disabled")), key=int)

    def slot_for_directory(self, cwd: str) -> tuple[str | None, str | None]:
        match = MappingStore(self.backup_dir).resolve(cwd)
        if not match:
            return None, None
        _, row = match
        return self._find_account_slot(self._get_sequence_data(), row["email"], row.get("organizationUuid", "")), row["email"]

    def list_mappings(self) -> None:
        for path, row in MappingStore(self.backup_dir).all().items():
            print(f"{path}  →  {row['email']}")

    def switch_to(self, identifier: str, json_output: bool = False, force: bool = False) -> dict:
        return self._switch_to(identifier, json_output, force, "direct")

    def _switch_to(self, identifier, json_output, force, strategy, expected_key=None) -> dict:
        self._guard_default()
        with self.lock(), auth_lock(self.live_path), contextlib.ExitStack() as leases:
            data = self._get_sequence_data()
            num = self._resolve(data, identifier)
            target = data["accounts"][num]
            if expected_key is not None and expected_key != target["identity"]:
                raise SwitchError("The target slot changed during selection. Retry the switch.")
            live_auth, live = self._read_live()
            outgoing = self._by_key(data, live.key) if live else None
            switched = not live or live.key != target["identity"]
            if switched:
                if live:
                    leases.enter_context(self.lease(live.key))
                leases.enter_context(self.lease(target["identity"]))
                auth = self._profile_auth(target)
                if live and not force:
                    if outgoing:
                        write_auth(self.account_home(live.key) / "auth.json", live_auth)
                    else:
                        self._stash(live_auth, live.email, "unmanaged-login")
                write_auth(self.live_path, auth)
            before = account_ref(int(outgoing) if outgoing else None, self.account_email(outgoing) if outgoing else live.email) if live else None
        message = f"{'Switched to' if switched else 'Already on'} Account-{num} ({target['email']})"
        if not json_output:
            print(message)
            if switched:
                print("Start a new Codex process to use this login.")
        return {
            "schemaVersion": SCHEMA_VERSION, "switched": switched, "from": before,
            "to": account_ref(int(num), target["email"]), "strategy": strategy,
            "reason": "switched" if switched else "already-active", "message": message,
            "warnings": ["Start a new Codex process to use this login."] if switched else [],
        }

    def switch(self, strategy=None, json_output=False, models=(), model_source=None) -> dict:
        data = self._get_sequence_data()
        rows = data["accounts"]
        current = self.current_account_number()
        candidates = sorted(
            (n for n, row in rows.items() if not row.get("disabled") and row["kind"] != "api_key"),
            key=lambda n: (int(n) <= int(current or 0), int(n)),
        )
        if not candidates:
            raise SwitchError("No enabled ChatGPT account is available. Select API-key accounts explicitly.")
        if strategy:
            fetch = set(candidates)
            if current is not None:
                fetch.add(current)
            usage = self.usage_entries_by_account(fetch=fetch)
            headroom = {
                n: oauth.account_headroom(entry.decision_value(), models)
                for n, entry in usage.items()
            }
            current_room = headroom.get(current)
            ranked = [(headroom[n], n) for n in candidates if n != current
                      and headroom.get(n) is not None and headroom[n] > 0]
            if strategy == "best":
                if current is not None and current_room is None:
                    return self._no_switch(rows, current, strategy, "usage-unavailable", json_output)
                if current_room is not None:
                    ranked = [(room, n) for room, n in ranked if room > current_room]
                ranked.sort(key=lambda entry: -entry[0])
            if not ranked:
                reason = "already-best" if current_room is not None and strategy == "best" else "no-valid-target"
                return self._no_switch(rows, current, strategy, reason, json_output)
            candidates = [n for _, n in ranked]
        candidate = candidates[0]
        return self._switch_to(candidate, json_output, False, strategy or "rotate", rows[candidate]["identity"])

    @staticmethod
    def _no_switch(rows, current, strategy, reason, json_output) -> dict:
        ref = account_ref(int(current), rows[current]["email"]) if current in rows else None
        message = {
            "already-best": "Already on the account with the most available quota.",
            "usage-unavailable": "Current account usage is unavailable.",
            "no-valid-target": "No other account has a current reading with quota available.",
        }[reason]
        if not json_output:
            print(message)
        return {"schemaVersion": SCHEMA_VERSION, "switched": False, "from": ref, "to": ref,
                "strategy": strategy, "reason": reason, "message": message, "warnings": []}

    def purge(self) -> None:
        self._guard_default()
        if not self.interactive or input("Delete all codex-swap data? [y/N] ").strip().lower() != "y":
            raise ValidationError("Purge requires confirmation in a terminal.")
        with self.lock(), contextlib.ExitStack() as leases:
            for row in self._get_sequence_data()["accounts"].values():
                leases.enter_context(self.lease(row["identity"]))
            # Keep the lock directory and sidecars stable for concurrent callers.
            self.sequence_file.unlink(missing_ok=True)
            for root in ("cache", "unclaimed"):
                path = self.backup_dir / root
                if path.exists():
                    shutil.rmtree(path)
            for name in ("settings.json", "mappings.json", "autoswitch_state.json", "menubar_settings.json", "codex-swap.log"):
                (self.backup_dir / name).unlink(missing_ok=True)
            for home in (self.backup_dir / "accounts").glob("*"):
                if not home.is_dir() or is_link(home):
                    continue
                for child in home.iterdir():
                    if child.name != ".owner.lock":
                        if child.is_dir() and not child.is_symlink() and not child.is_junction():
                            shutil.rmtree(child)
                        elif child.is_junction():
                            child.rmdir()
                        else:
                            child.unlink(missing_ok=True)
        print("Removed codex-swap data.")

    def set_poll_policy_inputs(self, threshold, weekly_threshold, models) -> None:
        self._poll_inputs_override = (threshold, weekly_threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None

    def _poll_inputs(self):
        if self._poll_inputs_override is not None:
            return self._poll_inputs_override
        settings = load_settings(self.backup_dir)
        return settings.threshold, settings.weekly_threshold, parse_model_names(settings.model)

    def refresh_account(self, number: str, email: str, *, force: bool = False) -> str:
        """Refresh an inactive account in place while holding its owner lease."""
        lease = None
        try:
            with self.lock():
                row = self.account_identity(number)
                if row.get("email") != email:
                    return "transient"
                _, live = self._read_live()
                if live and live.key == row["identity"]:
                    return "skip-live-login"
                candidate = self.lease(row["identity"])
                if not candidate.acquire():
                    return "skip-live-session"
                lease = candidate
                auth = self._profile_auth(row)
            expiry = oauth.access_expiry(auth)
            if not force and (expiry is None or expiry > time.time() + 300):
                return "ok"
            app_server.refresh(self.account_home(row["identity"]))
            successor = self._profile_auth(row)
            if successor == auth:
                return "transient"
            return "ok"
        except (CodexSwitchError, OSError):
            return "transient"
        finally:
            if lease is not None:
                lease.release()

    def freshen_target(self, number: str, email: str) -> str:
        row = self.account_identity(number)
        if not row or row["email"] != email:
            return "transient"
        if self.session_busy(row["identity"]):
            return "skip-live-session"
        if row["kind"] == "api_key":
            return "ok"
        return self.refresh_account(number, email)

    def _fetch_usage(self, num: str, row: dict) -> FetchRecord:
        try:
            live = self._read_live()
            auth = self.credential_for(row, live)
            expires = oauth.access_expiry(auth)
            if expires is not None and expires <= time.time():
                status = self.refresh_account(num, row["email"])
                if status != "ok":
                    return FetchRecord(error="access-token-expired")
                auth = self.credential_for(row, self._read_live())
            identity = identify(auth)
            usage = app_server.read_limits(auth, identity.workspace, identity.plan)
            return FetchRecord(usage=usage)
        except app_server.AppServerError as exc:
            return FetchRecord(error=exc.reason)
        except (CodexSwitchError, OSError):
            return FetchRecord(error="unavailable-credentials")

    @staticmethod
    def _cache_identity(row: dict) -> tuple[str, str]:
        return row["email"], row["identity"]

    def _usage_entries(self, rows: dict, active: str | None, fetch, scheduled) -> dict[str, UsageEntry]:
        identities = {n: self._cache_identity(row) for n, row in rows.items()}
        threshold, weekly_threshold, models = self._poll_inputs()
        before = self._usage_store.entries(identities, models=models)
        candidates = [n for n, row in rows.items()
                      if row["kind"] == "oauth" and (fetch is None or n in fetch)]
        # Claim only a batch the workers can start immediately.
        workers = 4
        plans = {}
        for offset in range(0, len(candidates), workers):
            claims = self._usage_store.reserve(
                candidates[offset:offset + workers], identities,
                respect_plans=fetch is None, repair_overslept=scheduled,
            )
            if not claims:
                continue
            with ThreadPoolExecutor(max_workers=len(claims)) as pool:
                jobs = {n: pool.submit(self._fetch_usage, n, rows[n]) for n in claims}
                outcomes = {n: job.result() for n, job in jobs.items()}
            now = self._usage_store.clock()
            for n, outcome in outcomes.items():
                if outcome.usage is not None:
                    line = self._policy(rows[n]).threshold or threshold
                    previous = before[n]
                    plans[n] = poll_policy.plan_after_fetch(
                        prev_interval_s=previous.poll_interval_s, prev_usage=previous.last_good,
                        new_usage=outcome.usage, is_active=n == active, threshold=line,
                        weekly_threshold=weekly_threshold, models=models,
                        recent_429=previous.recent_429(now), now=now,
                    )
            self._usage_store.record(outcomes, identities, claims=claims, plans=plans)
        entries = self._usage_store.entries(identities, models=models) if candidates else before
        for n, row in rows.items():
            if row["kind"] == "api_key":
                entries[n] = replace(entries[n], sentinel=USAGE_API_KEY)
            elif entries[n].last_error == "access-token-expired":
                # Do not act on last-good quota when this grant requires renewal.
                entries[n] = replace(entries[n], sentinel=USAGE_TOKEN_EXPIRED)
        return entries

    def usage_entries_by_account(self, fetch=None, *, scheduled=False) -> dict[str, UsageEntry]:
        data = self._get_sequence_data()
        _, live = self._read_live()
        active = self._by_key(data, live.key) if live else None
        return self._usage_entries(data["accounts"], active, fetch, scheduled)

    def accounts_snapshot(self, fetch=None) -> AccountsSnapshot:
        data = self._get_sequence_data()
        try:
            _, live = self._read_live()
        except CredentialReadError:
            if fetch != set():
                raise
            live = None
        active = self._by_key(data, live.key) if live else None
        rows = data["accounts"]
        entries = self._usage_entries(rows, active, fetch, False)
        accounts = []
        for n, row in sorted(rows.items(), key=lambda item: int(item[0])):
            plan = live.plan if live and n == active else row.get("plan")
            record = {"plan": {"tier": plan}, "fetchedAt": row.get("updatedAt", 0)} if plan else None
            accounts.append(AccountSnapshot(
                number=n, email=row["email"], org_name="", org_uuid=row.get("organizationUuid", ""),
                is_active=n == active, kind=row["kind"], switchable=True,
                usage=entries[n], alias=row.get("alias", ""), disabled=bool(row.get("disabled")),
                policy=self._policy(row), subscription_plan=record,
            ))
        return AccountsSnapshot(active, tuple(accounts), self._usage_store.clock())

    def refresh_subscription_plans(self) -> None:
        """Plan labels come from the stored login and are read with snapshots."""

    def _json_row(self, account: AccountSnapshot, now: float) -> dict:
        entry = account.usage
        return account_row(
            int(account.number), account.email, account.org_name, account.org_uuid, account.is_active,
            entry.decision_value(), usage_fetched_at=entry.fetched_at, usage_age_s=entry.age_s,
            last_good_usage=entry.last_good, last_error=entry.last_error,
            backoff_until=entry.backoff_until if entry.in_backoff(now) else None,
            alias=account.alias, disabled=account.disabled, policy=account.policy,
            subscription_plan=account.subscription_plan,
        )

    @staticmethod
    def _print_account(account: AccountSnapshot) -> None:
        tag = f" ({account.alias})" if account.alias else ""
        plan = f" · {account.subscription_plan['plan']['tier']}" if account.subscription_plan else ""
        print(f"{'*' if account.is_active else ' '} {account.number}  {account.email}{tag}{plan}")
        usage = account.usage
        if usage.sentinel:
            print(f"    {SENTINEL_NOTES.get(usage.sentinel, usage.sentinel)}")
        elif usage.last_good:
            values = (f"{name}: {pct:.0f}%"
                      + (f" · resets {oauth.format_reset(reset)[1]}" if reset else "")
                      for name, pct, reset in oauth.relevant_windows(usage.last_good, ("all",)))
            print("    " + "  ".join(values))
            if (note := last_seen_note(usage)) and not usage.fresh(time.time()):
                print(f"    {note}")
        else:
            print(f"    Usage unavailable{f' ({usage.last_error})' if usage.last_error else ''}")

    def list_accounts(self, show_token_status=False, json_output=False) -> dict | None:
        snap = self.accounts_snapshot()
        if json_output:
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "activeAccountNumber": int(snap.active_number) if snap.active_number else None,
                "accounts": [self._json_row(account, snap.taken_at) for account in snap.accounts],
            }
            if entries := self.list_unclaimed_credentials():
                payload["unclaimedCredentials"] = sorted(entries)
            return payload
        if not snap.accounts:
            print("No accounts saved. Log into Codex, then run codexswap add.")
        for account in snap.accounts:
            self._print_account(account)
            if show_token_status:
                credential = self.read_account_credentials(account.number, account.email)
                expires = oauth.access_expiry(json.loads(credential)) if credential else None
                if expires is not None:
                    try:
                        print(f"    Access token expires {oauth.login_expiry_clock(expires)}")
                    except (ValueError, OverflowError, OSError):
                        print("    Access token expiry unavailable.")
        return None

    def status(self, json_output=False) -> dict | None:
        _, live = self._read_live()
        current = self.current_account_number()
        snap = self.accounts_snapshot(fetch={current}) if current else None
        account = next((a for a in snap.accounts if a.is_active), None) if snap else None
        if json_output:
            payload = {"schemaVersion": SCHEMA_VERSION, "active": None}
            if account:
                row = self._json_row(account, snap.taken_at)
                row.pop("active")
                payload.update(active={**row, "managed": True}, totalManagedAccounts=len(snap.accounts))
            elif live:
                payload["active"] = {"email": live.email, "managed": False}
            return payload
        if account:
            self._print_account(account)
        elif live:
            print(f"Active login: {live.email} (unmanaged).")
        else:
            print("No active Codex login.")
        return None
