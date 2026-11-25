"""Custom exceptions for Codex Swap."""


class CodexSwitchError(Exception):
    """Base exception for Codex Swap errors."""

    pass


class CredentialError(CodexSwitchError):
    """Error related to credential operations."""

    pass


class CredentialReadError(CredentialError):
    """Failed to read credentials."""

    pass


class CredentialWriteError(CredentialError):
    """Failed to write credentials."""

    pass


class ConfigError(CodexSwitchError):
    """Error related to configuration operations."""

    pass


class SwitchError(CodexSwitchError):
    """Error during account switch operation."""

    pass


class SessionError(CodexSwitchError):
    """Error setting up or launching a session-mode profile."""

    pass


class LockError(CodexSwitchError):
    """Error acquiring lock."""

    pass


class ClaudeCodeLockTimeout(LockError):
    """Timed out acquiring one of Codex's own advisory locks.

    Raised when ``~/.codex.lock`` / ``~/.codex.json.lock`` stays held past
    our bounded wait — usually Codex mid-token-refresh. Nothing has been
    mutated when this raises; the operation is safe to retry.
    """

    pass


class AccountNotFoundError(CodexSwitchError):
    """Account not found."""

    pass


class ValidationError(CodexSwitchError):
    """Validation error."""

    pass


class TransferError(CodexSwitchError):
    """Error during account export or import."""

    pass


class MigrationError(CodexSwitchError):
    """Error migrating the backup directory between layouts (e.g. legacy → XDG)."""

    pass


class MigrationIncomplete(CodexSwitchError):
    """A one-time data migration could not finish for every record.

    Raised by run-once migrations (see ``migrations.py``) when some entries
    failed or the source backend was inaccessible. The migration runner treats
    this as "not applied" so the migration is retried on the next run rather
    than being recorded as done with records left behind.
    """

    pass
