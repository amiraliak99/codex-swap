# codex-swap

Manage multiple OpenAI Codex CLI accounts with numbered slots, quota-aware switching, isolated sessions, a terminal dashboard, and a macOS menu bar. Adapted from [claude-swap](https://github.com/realiti4/claude-swap), with the original MIT license.

## Install

Requires Python 3.12+ and the `codex` executable on PATH. The credential format, locks and app-server protocol were checked against [Codex CLI 0.172.0](https://github.com/openai/codex/tree/rust-v0.172.0).

```bash
git clone git@github.com:amiraliak99/codex-swap.git
cd codex-swap
uv tool install .
# Or run from the checkout: uv run codexswap help
```

To update a checkout, run `git pull --ff-only` and reinstall with `uv tool install --force .`. The long command name is `codex-swap`.

## Add and switch accounts

```bash
codex login
codexswap add --alias work                    # save the current login
codexswap add --login --alias personal        # sign into another account separately
codexswap add --login --device-auth          # use Codex's device flow if enabled
codexswap list                               # accounts, usage, reset times
codexswap switch personal                    # switch by slot, email or alias
codexswap switch                             # rotate to the next enabled ChatGPT login
codexswap switch --strategy best             # pick the most available quota
codexswap status --json
```

Set `cli_auth_credentials_store = "file"` in the default Codex `config.toml` if you changed the credential storage setting. This version supports managed ChatGPT logins and stored OpenAI API keys. Keyring, externally managed auth and desktop account switching are outside the supported path.

Exit default-profile Codex processes before changing the default login. Start a new Codex process after switching. An existing native Codex process can keep using its cached login and may have a token refresh in flight. Use isolated sessions for accounts you want to run concurrently. To continue the same conversation after switching, exit Codex and run `codex resume --last` from the same directory.

## Sessions and directory mappings

```bash
codexswap run personal
codexswap run personal -- resume --last
codexswap run personal --no-share --no-share-history
codexswap map work ~/work
cd ~/work
codexswap run
```

Each inactive account has its own `CODEX_HOME`. By default sessions copy the default config and share the usual skills, rules and history files. `--no-share` and `--no-share-history` control those independently. SQLite databases stay per home. Private history is retained when shared history is enabled. Codex running on the currently active account uses the default profile, and `--require-session` requires isolation.

## Dashboards and automatic switching

```bash
codexswap tui
codexswap watch
codexswap auto --once --dry-run --json
codexswap auto --threshold 90 --weekly-threshold 97
codexswap config set autoswitch.strategy priority-consume-first
codexswap priority work 100
codexswap threshold personal 85
codexswap disable personal
```

Interactive `codexswap` also opens the dashboard. On macOS install `uv tool install '.[menubar]'` from the checkout and run `codexswap menubar`. See [auto-switch](docs/auto-switch.md), [menu bar](docs/menubar.md) and the [JSON schema reference](docs/json-output.md).

## Back up and restore

```bash
codexswap export backup.codexswap
codexswap import backup.codexswap
codexswap list --json > usage.json
codexswap import-usage usage.json --hold 600
```

Credential backups contain reusable secrets in plaintext. Keep them private. Imports keep existing credentials unless you pass `--force`, and leave the live login in place. Add API keys with `codexswap add-token` for hidden input or `codexswap add-token -` for stdin. API-key accounts have no ChatGPT quota readout.

## Data locations

| Data | Location |
| --- | --- |
| Codex CLI login | `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/codex-swap` |
| macOS | `~/Library/Application Support/codex-swap` |
| Windows | `%LOCALAPPDATA%\codex-swap` |

Set `CODEX_SWAP_HOME` to use a different swap store. See the [architecture](docs/architecture.md) for credential ownership and locking.
