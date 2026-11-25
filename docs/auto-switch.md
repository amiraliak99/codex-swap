# Auto-switch

`codexswap auto` evaluates the active managed login against usage thresholds and switches the default login to an eligible stored account. Use `--once` for a scheduled job and `--dry-run` to see decisions without publishing a switch.

| Strategy | Behavior |
| --- | --- |
| `best` | Switch at the threshold to the account with most quota headroom |
| `weekly-first` | Prefer a usable account with the earliest weekly reset |
| `consume-first` | Spend sooner-expiring weekly quota when it has enough headroom |
| `priority-consume-first` | Prefer higher-priority usable accounts, then sooner weekly resets |

Use `codexswap threshold ACCOUNT PCT`, `codexswap priority ACCOUNT TIER` and `codexswap disable ACCOUNT` to change per-account policy. `codexswap config list` shows global defaults. `--weekly-threshold` sets a separate threshold for weekly windows. `--model all` includes the named weekly model buckets returned by Codex. Supply a comma-separated list to target specific bucket names shown by `codexswap list`.

Reads come from the Codex app-server rate-limit response. Missing windows stay unavailable. The scheduler uses cached data within bounded freshness and applies a cooldown and hysteresis to reduce rapid switching. API-key accounts are excluded by default and can be a fallback with `--include-api-key-accounts`.

An account owned by an active codex-swap session is unavailable as a switch target. An expired default login is left for native Codex to renew. Exit independent default-profile Codex clients before turning on automatic switching. New processes pick up the selected account.

`auto --once` returns 0 when a switch was selected, 1 on error, 2 when nothing needs changing and 3 when switching was blocked. `--json` emits one event per line.
