"""Back up Codex logins and transfer usage measurements."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from codex_swap.credentials import auth_lock, identify
from codex_swap.exceptions import TransferError
from codex_swap.json_output import SCHEMA_VERSION as JSON_SCHEMA_VERSION, usage_from_json
from codex_swap.switcher import CodexAccountSwitcher

FORMAT = "codex-swap-backup"


def _eprint(text: str) -> None:
    print(text, file=sys.stderr)


def _parse_payload(text: str, label: str) -> dict:
    try:
        result = json.loads(text)
    except ValueError:
        raise TransferError(f"Invalid JSON in {label}.") from None
    if not isinstance(result, dict):
        raise TransferError(f"Expected a JSON object in {label}.")
    return result


def export_accounts(switcher, destination, account=None) -> None:
    with switcher.lock(), auth_lock(switcher.live_path):
        data = switcher._get_sequence_data()
        live = switcher._read_live()
        selected = [switcher._resolve(data, account)] if account else data["accounts"]
        entries = []
        for number in selected:
            row = data["accounts"][number]
            entries.append({
                "number": int(number), "email": row["email"],
                "alias": row.get("alias"), "auth": switcher.credential_for(row, live),
            })
    document = {"format": FORMAT, "schemaVersion": 1, "accounts": entries}
    if destination == "-":
        raise TransferError("Choose a file for the credential export.")
    dest = Path(destination).expanduser()
    payload = json.dumps(document, indent=2) + "\n"
    created = False
    try:
        descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            dest.unlink(missing_ok=True)
        raise TransferError("Could not create the export. Choose a new writable file.") from None
    _eprint(f"Exported {len(entries)} account(s). Keep this credential file private.")


def import_accounts(switcher, source, force=False) -> None:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TransferError("Could not read credential backup.") from None
    document = _parse_payload(text, "credential backup")
    if document.get("format") != FORMAT or document.get("schemaVersion") != 1:
        raise TransferError("Unsupported Codex credential backup format.")
    rows = document.get("accounts")
    if not isinstance(rows, list):
        raise TransferError("Credential backup is missing its accounts list.")
    validated = []
    seen = set()
    for row in rows:
        auth = row.get("auth") if isinstance(row, dict) else None
        if not isinstance(auth, dict):
            raise TransferError("A backup entry has invalid credentials.")
        identity = identify(auth)
        number = row.get("number")
        alias = row.get("alias")
        email = row.get("email") if identity.kind == "api_key" else None
        if (type(number) is not int or not 1 <= number <= 999_999_999
                or (alias is not None and not isinstance(alias, str))
                or (email is not None and (not isinstance(email, str) or
                                          not email.isprintable() or "@" not in email))
                or identity.key in seen):
            raise TransferError("A backup entry has invalid slot metadata or a duplicate identity.")
        seen.add(identity.key)
        validated.append((number, auth, identity, alias, email))
    written = 0
    with switcher.lock(), auth_lock(switcher.live_path):
        _, live = switcher._read_live()
        for number, auth, identity, alias, email in validated:
            data = switcher._get_sequence_data()
            rows = data["accounts"]
            existing = switcher._by_key(data, identity.key)
            if (live and live.key == identity.key) or (existing and not force):
                continue
            slot = int(existing) if existing else number if str(number) not in rows else None
            switcher._register_auth(auth, identity, slot, alias, email, False)
            written += 1
    _eprint(f"Imported {written} account(s), kept {len(validated) - written} local login(s).")


def import_usage(
    switcher: CodexAccountSwitcher,
    source: str,
    hold_s: float | None = None,
) -> None:
    """Adopt usage readings another machine took, from its ``codexswap list --json``.

    For machines that hold the same accounts: when they share a login (moved
    with ``export``/``import``) or the account is budgeted per account rather
    than per token (see poll_policy), every machine polling it spends one
    usage-endpoint budget, so a script can let one machine poll and hand its
    readings to the rest.

    Each row with decision-grade usage (``usageStatus`` "ok") is matched to a
    local slot by (email, organizationUuid), the key ``import`` uses, and
    handed to :meth:`UsageStore.adopt` with ``usageAgeSeconds`` as its age.
    Rows without usage, and accounts not managed here, are skipped. With
    ``hold_s``, no local collector fetches the matched accounts for that long
    (bounded; see ``adopt``); 0 lifts an earlier hold, ``None`` keeps it.

    Args:
        switcher: Initialized CodexAccountSwitcher.
        source: File path, or "-" for stdin.
        hold_s: Seconds no local collector may fetch the matched accounts,
            0 to lift an earlier hold, None to leave it as it is.

    Raises:
        TransferError: malformed document or unsupported schema version.
    """
    if source == "-":
        text = sys.stdin.read()
    else:
        in_path = Path(source).expanduser()
        if not in_path.exists():
            raise TransferError(f"usage file not found: {in_path}")
        text = in_path.read_text(encoding="utf-8")

    document = _parse_payload(text, "usage document")
    version = document.get("schemaVersion")
    if version != JSON_SCHEMA_VERSION:
        raise TransferError(
            f"unsupported usage document schemaVersion: {version!r} "
            f"(expected {JSON_SCHEMA_VERSION}, as printed by 'codexswap list --json')"
        )
    rows = document.get("accounts")
    if not isinstance(rows, list):
        raise TransferError("usage document has no accounts list")

    # Validate every row before writing any, as import_accounts does: a
    # malformed row later in the list must not leave earlier ones adopted.
    local_data = switcher._get_sequence_data() or {}
    readings: dict[str, tuple[dict, float]] = {}
    identities: dict[str, tuple[str, str]] = {}
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            raise TransferError("each account row must be a JSON object")
        email = row.get("email")
        org_uuid = row.get("organizationUuid") or ""
        if not isinstance(email, str) or not email or not isinstance(org_uuid, str):
            raise TransferError("each account row needs an email and an organizationUuid")
        if row.get("usageStatus") != "ok":
            skipped += 1
            continue
        age_s = row.get("usageAgeSeconds")
        if (
            not isinstance(age_s, (int, float))
            or isinstance(age_s, bool)
            or not math.isfinite(age_s)
            or age_s < 0
        ):
            raise TransferError(
                f"usageAgeSeconds for {email} must be a non-negative number"
            )
        try:
            usage = usage_from_json(row.get("usage"))
        except ValueError as exc:
            raise TransferError(f"usage for {email}: {exc}")
        num = switcher._find_account_slot(local_data, email, org_uuid)
        if num is None:
            skipped += 1
            continue
        if num in readings:
            raise TransferError(
                f"duplicate account in usage document: {email} "
                f"(org={org_uuid or 'personal'})"
            )
        readings[num] = (usage, float(age_s))
        identities[num] = switcher._cache_identity(local_data["accounts"][num])

    adopted = switcher._usage_store.adopt(readings, identities, hold_s=hold_s)
    for num in identities:
        email = local_data["accounts"][num]["email"]
        if num in adopted:
            _eprint(f"Adopted usage for {email} → slot {num}")
        else:
            _eprint(f"Kept slot {num}'s own reading for {email}: it is newer")
    summary = (
        f"Done: {len(adopted)} adopted, {len(readings) - len(adopted)} kept, "
        f"{skipped} skipped"
    )
    if hold_s is not None and readings:
        summary += (
            f"; fetching held for up to {hold_s:.0f}s" if hold_s > 0 else "; holds lifted"
        )
    _eprint(summary)
