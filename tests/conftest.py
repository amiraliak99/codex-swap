"""Keep tests off the real credential stores and mock networked Codex requests."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from codex_swap import app_server, paths

protected = tuple(path.resolve() for path in (
    paths.get_codex_home(), paths.get_backup_root(),
    Path.home() / ".codex", Path.home() / ".claude",
    Path.home() / ".local" / "share" / "claude-swap",
))


def guard(event, args):
    if event == "open":
        if not args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            return
        targets = args[:1]
    elif event in ("os.rename", "os.link"):
        targets = args[:2]
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod"):
        targets = args[:1]
    elif event == "os.symlink":
        targets = args[1:2]
    else:
        return
    for target in targets:
        if not isinstance(target, (str, bytes, os.PathLike)):
            continue
        candidate = Path(os.fsdecode(target)).absolute()
        if any(candidate.is_relative_to(root) for root in protected):
            raise SystemExit("A test tried to modify a real credential store.")
        try:
            path = candidate.resolve()
        except (RuntimeError, OSError):
            continue
        if any(path.is_relative_to(root) for root in protected):
            raise SystemExit("A test tried to modify a real credential store.")


sys.addaudithook(guard)


@pytest.fixture
def temp_home(tmp_path):
    test_home = tmp_path / "home"
    test_home.mkdir(exist_ok=True)
    return test_home


@pytest.fixture(autouse=True)
def isolate(tmp_path_factory, monkeypatch):
    temp_home = tmp_path_factory.mktemp("credential-home")
    monkeypatch.setenv("HOME", str(temp_home))
    monkeypatch.setenv("USERPROFILE", str(temp_home))
    monkeypatch.setenv("CODEX_HOME", str(temp_home / ".codex"))
    monkeypatch.setenv("CODEX_SWAP_HOME", str(temp_home / "swap"))
    for name in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_SWAP_SESSION"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*_args, **_kwargs):
        raise AssertionError("Mock Codex network calls in tests.")

    monkeypatch.setattr(app_server, "read_limits", no_network)
    monkeypatch.setattr(app_server, "refresh", no_network)


def jwt(claims: dict) -> str:
    def encode(payload):
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.fake"


def login_record(user: str, *, workspace="workspace", expires=4_000_000_000, generation="1") -> dict:
    from codex_swap.credentials import AUTH_CLAIM, PROFILE_CLAIM

    email = f"{user}@example.test"
    auth = {
        "chatgpt_account_id": workspace, "chatgpt_user_id": user,
        "chatgpt_plan_type": "plus",
    }
    return {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt({"email": email, AUTH_CLAIM: auth}),
            "access_token": jwt({"exp": expires, AUTH_CLAIM: auth, PROFILE_CLAIM: {"email": email}}),
            "refresh_token": f"synthetic-{user}-{generation}", "account_id": workspace,
        },
    }


@pytest.fixture(autouse=True)
def _deterministic_poll_jitter(monkeypatch):
    """Zero the poll-plan jitter so cadence tests are clock-exact; the jitter
    itself is exercised in test_poll_policy via an injected rng."""
    monkeypatch.setattr("codex_swap.poll_policy.JITTER_FRAC", 0.0)


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    """Durability is not under test, and one real fsync per atomic write is
    most of the suite's I/O. Tests that inject at fsync patch over this."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture(autouse=True)
def _deterministic_colour(monkeypatch):
    """A developer's terminal must not decide whether the suite passes.

    ``printer._detect_color_support`` honours ``FORCE_COLOR``/``NO_COLOR``
    before it consults ``isatty()`` — correct for the CLI, where those
    variables exist to override detection, and wrong under pytest, where
    stdout is captured and every assertion on plain output then breaks.
    Measured with ``FORCE_COLOR=3`` exported: 11 failures in test_switcher.py
    reading ``assert 'Skipping Account-2 (disabled)' in
    '\\x1b[38;5;173mSkipping\\x1b[0m Account-2 (disabled)'``, and the same
    tree green with the variable unset.

    Scrubbing the variables is not enough on its own, because detection
    CACHES. ``colors_enabled()`` latches the first answer into
    ``printer._colors_enabled`` and every later call returns it, so one test
    that latches ``True`` decides the styling for every test after it —
    whatever the environment says by then. Under ``pytest -s`` stdout stays
    the real terminal, so ``isatty()`` is True and ordinary tests latch it
    without meaning to. So the cache is reset too, on every entry, not just
    the two environment variables.

    A wrong reset can satisfy a shape check that only looks at "not latched":
    pinning `False` instead of `None` also leaves every test entering
    unlatched, while having switched from un-latching to PINNING — a
    different, wrong mechanism with the same signature. Both directions are
    covered: `test_printer.py`'s `TestColourEnvDoesNotLeakIntoTests` dies
    under either pin, not just under "reset removed".

    A number nobody can re-take is not evidence — this fixture's own comments
    went stale from hand-copied counts more than once, which is why none are
    kept here now. What's checked instead is the SHAPE: mutating the reset
    out flips the suite from green to broadly red, and both directions are
    exercised by real tests rather than recorded as a figure.

    Tests that exercise the detection itself set the variables explicitly,
    which overrides this.
    """
    # Ordering-PROOF half of the guard. `test_colour_cache_isolation.py`'s
    # latch/read pairs read as the guard, but they only fire in one ordering:
    # a run that selects only the reader, or splits the pair across workers,
    # is green over a broken fixture. These assertions run before EVERY
    # test, so no test ordering hides a latch — only a fixture that latches
    # during SETUP AFTER this one escapes them (a conftest fixture runs
    # before a module-level autouse fixture of the same scope). That is
    # exactly the shape of the two module-local fixtures this branch
    # deletes, so the fix is to not have them.
    from codex_swap import appearance as _appearance
    from codex_swap import printer as _printer

    # SNAPSHOT FIRST, ASSERT ON THE SNAPSHOT. The obvious form — asserting the
    # globals directly — depends on sitting ABOVE the resets, and nothing in
    # the code says so: moving the two `setattr` lines up would make each
    # assert read the value the reset just wrote, silently passing regardless
    # of what the previous test left behind. Reading into a local on the
    # fixture's first statement makes the detection independent of where the
    # assertions live, and is itself mutation-tested directly (not via
    # ordering) by `TestEntryAssertionsCatchAPoisonedGlobal` in
    # `test_printer.py`.
    inherited = (_printer._colors_enabled, _printer._theme)
    assert inherited[0] is None, (
        "a previous test latched the colour cache and nothing reset it, or a "
        "broader-scoped fixture latched it during setup"
    )
    assert inherited[1] == "dark", (
        "a previous test latched the theme, or a broader-scoped fixture "
        "latched it during setup"
    )
    # Both scrubs are killed individually ON A CLEAN ENVIRONMENT, which is the
    # only coverage worth having: with neither variable exported and both
    # lines deleted, the whole suite is green — so before
    # `test_printer.py`'s class-scoped `_exported` fixture these two lines
    # were dead code on every CI runner, guarded only on a box where the bug
    # was already happening. Drop either one now and its own test dies.
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("codex_swap.printer._colors_enabled", None)
    # The OTHER latched global in the same module. `tui/app.py` calls
    # `printer.set_theme("light")`, a plain assignment with nothing restoring
    # it, so with this line removed the tests after it enter with the light
    # palette — spread across several files, not just the one that latched.
    # Green today only because none of them asserts a palette code, which is
    # exactly what was true of `_colors_enabled` until a developer exported
    # FORCE_COLOR.
    monkeypatch.setattr("codex_swap.printer._theme", "dark")
    # The third `global`-rebound name in the package. Inert under the TERM pin
    # below, which makes `detect_terminal_background` return before ever
    # writing it — but the pin is a guarantee about the tty QUERY, not about
    # the cache, and the cache then latches that `None` against any test that
    # stubs `_query_terminal_background`. `tests/test_appearance.py` carries
    # a module-local reset for exactly this reason, which is the layer this
    # fixture exists to replace.
    monkeypatch.setattr("codex_swap.appearance._cache", _appearance._UNSET)
    # And the OTHER thing a terminal decides: `appearance.detect_terminal_background`
    # puts the tty into cbreak, writes an OSC-11 query, and BLOCKS reading stdin
    # for up to a second. Under `pytest -s` stdin is the developer's real
    # terminal, so the suite emits escape bytes at it and can swallow a
    # keypress.
    #
    # `TERM=dumb` is the function's own documented short-circuit — it returns
    # None before touching termios — so this closes the path rather than
    # patching around it. It is a guarantee about the QUERY and not about the
    # cache, which is why the cache is reset above: the pin makes detection
    # return None fast, and the cache then latches that None against any test
    # that stubs `_query_terminal_background`. No test outside
    # test_appearance.py asserts on a palette code, so this was not flipping
    # results; it is the same class this fixture exists for, one module over.
    #
    # Stated as a magnitude, not a count, on purpose: the number of tests that
    # reach detection moves with every commit, so what stays true and
    # re-takeable is "zero termios entries with this pin, many without it,
    # and the suite runs measurably longer without it" (the OSC-11 read
    # timing out over and over).
    #
    # Pinned by test_the_suite_does_not_query_the_developers_terminal, which
    # opens a real pty so both isatty() calls are genuinely True and the TERM
    # check at appearance.py:97 is the only gate left, then reads the master
    # to see whether the query went out.
    #
    # That test also unsets TMUX and STY, deliberately: appearance.py:99
    # short-circuits on either, so a developer running inside tmux would get
    # a green from the WRONG gate and the TERM pin would look load-bearing
    # when it was not. This fixture does not pin TMUX/STY — nothing needs it
    # while TERM is pinned first — but the guard test has to strip them or it
    # proves nothing about the line it guards.
    monkeypatch.setenv("TERM", "dumb")


def _advancing_clock(clock, budget):
    """A scripted `monotonic` that MOVES ON READ.

    A clock advanced only inside the fake sleep parks on one instant the moment
    the code under test stops sleeping, so the retry loop never reaches its
    deadline and the case HANGS instead of failing -- which is worse than the
    bug, because a hung xdist worker holds the job with nothing to read.

    A FIXED hair does not scale: at a 3s budget it needs three million
    iterations, and the stale-takeover fires first, so the case reports the
    wrong failure. The step itself is set below, with why.
    """
    # SMALL ENOUGH NOT TO PERTURB. A thousandth of the budget shifts the
    # remainders these cases assert on; a hundred-thousandth bounds a
    # sleepless loop at ~100k reads (a fraction of a second) and leaves
    # every measured remainder unchanged.
    step = budget / 100000.0
    # NOT THREAD-SCOPED, and it cannot be: the heartbeat under test runs on
    # its own thread and must see the clock move (scoping it fails 6 cases,
    # because the subject IS a thread). The asymmetry with the `fake_sleep`
    # beside it: a read from an unrelated thread advances this clock too, and
    # the remainder assert in `test_no_retry_sleep_outlives_the_budget_it_was_
    # given` would then accuse pristine code. This suite has no such reader.

    def monotonic():
        clock[0] += step
        return clock[0]

    return monotonic


def _thread_scoped_sleep(module, clock, slept, budget=None, step=0.0):
    """A recorder for ``module.time.sleep`` bound to the calling thread.

    The patch is process-global, so an unscoped recorder writes every other
    thread's sleeps into the sample and spins them through a sleep that never
    sleeps. Records ``(remaining, seconds)`` when ``budget`` is given, else
    ``seconds``, and advances ``clock`` by the sleep plus ``step`` -- one
    iteration of work, so a clamped 0.0 cannot park a scripted clock on the
    deadline.
    """
    mine = threading.get_ident()
    real = module.time.sleep

    def fake_sleep(seconds):
        if threading.get_ident() != mine:
            return real(seconds)
        if budget is None:
            slept.append(seconds)
        else:
            slept.append((round(budget - clock[0], 3), round(seconds, 3)))
        clock[0] += seconds + step

    return fake_sleep


def _crossing_clock(reads):
    """0.0 twice (start, deadline check), then 0.6 forever: the clamp's own
    read lands past a 0.5 deadline. Every read is appended to ``reads``."""
    ticks = iter([0.0, 0.0])

    def clock():
        tick = next(ticks, 0.6)
        reads.append(tick)
        return tick

    return clock
