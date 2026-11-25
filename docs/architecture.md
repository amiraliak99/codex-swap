# Architecture

`paths.py` resolves the Codex CLI home and a separate codex-swap store. `credentials.py` understands the file-backed credential format from [Codex CLI 0.172.0](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/login/src/auth/storage.rs). It identifies a managed ChatGPT grant by user and workspace, or an API key by its fingerprint.

`switcher.py` owns numbered slots, aliases, policy, credentials and snapshots. Slot metadata lives in `sequence.json`. Each account has a stable identity-derived home under `accounts/`, so moving a slot does not move the credential or session files. Unmanaged outgoing credentials are retained in `unclaimed/`. These files contain secrets.

The active account's authoritative grant is Codex's live auth file. On switch, codex-swap locks its registry, takes Codex's auth locks and acquires the relevant account owner leases without waiting. It verifies the target, checkpoints the outgoing login and atomically replaces the live file. Active state is inferred from the live file. A failure publishing the target leaves the live login and outgoing checkpoint available.

`session.py` launches Codex with the account's own `CODEX_HOME` and holds its owner lease until the subprocess exits. An inactive account's profile is the authoritative grant during its session, and also the place where native proactive refresh writes its successor. Switch operations do no network requests under the registry or native commit locks. Native clients outside codex-swap do not hold its owner leases, so exit default clients before switching.

`app_server.py` uses Codex's stdio JSON-RPC. Usage reads provide only the access token in an ephemeral profile and call `account/rateLimits/read`. When an inactive login needs renewal, `account/read` requests a managed refresh in the authoritative file-backed profile under its exclusive owner lease. The active account is refreshed by its native Codex client.

`UsageStore` fences requests with leases, records successful readings and backs off after errors. `poll_policy.py` controls polling cadence. `AutoSwitchEngine` consumes snapshots and renders typed events through its caller. `SnapshotSource` serves the TUI and menu bar. The CLI, TUI and menu bar share the switcher and engine. `transfer.py` handles credential backups and portable usage snapshots.

Run tests with `uv sync --locked` then `uv run pytest`. The suite uses temporary homes and blocks writes to the real credential stores.
