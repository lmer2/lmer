"""The assistant runs on its own credential, minted per incarnation (issue #244).

Until this, the assistant was handed the *operator's* shared secret, and the
consequence was not a privilege question — it holds the same authority either way
— but an identity one: every request the platform received looked identical, so
"has anybody actually looked at this run?" had no answer. The browser polls a
session's routes every five seconds while a pane is open, so any attempt to infer
the assistant from traffic would have let an open tab suppress the reminders the
whole check-in mechanism exists to produce.

So the platform mints one. What is pinned here:

- **It is minted, not shared.** A start writes a fresh value into a 0600 file and
  into the session's ``.env``; the shared secret is not what the container gets.
- **It authenticates, and it is attributed.** Both credentials open the API, and
  the guard says which arrived — the distinction #244's check-in tracking reads.
- **It dies with the incarnation.** A stop revokes it, so a stopped assistant's
  environment file is no longer a live key; a rotation replaces it, so the
  predecessor's copy stops working the moment the successor starts.
- **It never travels in argv, and a live one does not survive a transcript.** The
  spawn command carries a *path*, and the read-path scrub strikes the value the
  way it strikes the shared secret — this is the credential that reaches a
  container whose conversation the chat view renders to a browser. The boundary is
  pinned too: once the incarnation is stopped there is no value left to mask, and
  what a transcript can still hold is a token that opens nothing.
- **A mint that fails costs attribution, not the chat window.** The assistant
  falls back to the shared secret and runs exactly as it did before.

The stub-lifetime trap that every assistant test carries applies here too: a stub
that exits cleanly has its registry entry reaped, so anything asserting on a
*running* assistant sets ``FAKE_LMER_SLEEP`` and kills the process afterwards.
"""

import contextlib
import os
import pathlib
import stat
import time

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, assistant, registry, store, transcripts
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-shared-secret-value"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_REPO_URL", "FAKE_LMER_SLEEP",
                 "FAKE_LMER_EXIT", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT,
                 cfg.ENV_CONTAINER_URL, cfg.ENV_SECRET_FILE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer, monkeypatch):
    """A configuration whose container URL is set, so a credential is issued.

    ``_assistant_environment`` writes the pair or neither, and the pair needs a
    URL a container could dial — which this host cannot derive under a test.
    """
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "http://platform.test:8600")
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def long_lived(monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")


def kill(pid):
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


def wait_for(predicate, timeout=5.0):
    """Poll until *predicate* holds — a child writes on its own schedule."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@contextlib.contextmanager
def started(config, **kwargs):
    status = assistant.start(config, **kwargs)
    try:
        yield status
    finally:
        kill(status.pid)


def env_values():
    """The assistant's ``.env`` as a mapping."""
    values = {}
    for line in assistant.env_file_path().read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name] = value.strip().strip('"')
    return values


def client_for(config):
    return TestClient(
        api.create_app(
            config, SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- minting -----------------------------------------------------------------

def test_a_start_mints_a_credential_that_is_not_the_shared_secret(
    config, long_lived
):
    shared = cfg.ensure_secret(config)
    with started(config):
        minted = cfg.active_assistant_credential()
        assert minted, "a start must mint a credential"
        assert minted != shared, (
            "the assistant holding the operator's own key is what #244 replaced"
        )
        assert env_values()[assistant.ENV_PLATFORM_CREDENTIAL] == minted


def test_the_credential_file_is_owner_only(config, long_lived):
    cfg.ensure_secret(config)
    with started(config):
        mode = stat.S_IMODE(cfg.assistant_credential_path().stat().st_mode)
        assert mode == 0o600, f"credential file is {mode:o}, must be 0600"


def test_the_credential_never_reaches_the_command_line(config, long_lived):
    """argv is echoed over HTTP and written to events.jsonl; a path is not a key.

    Asserted on the child's *own* argv — the stub echoes ``$*`` into its log — so
    this is what the process was really given, rather than a restatement of the
    request this module built.
    """
    cfg.ensure_secret(config)
    with started(config) as status:
        minted = cfg.active_assistant_credential()
        entry = registry.read_session(status.session_id)
        log = pathlib.Path(entry["log_path"])
        assert wait_for(lambda: log.is_file() and log.stat().st_size), (
            "the assistant's child never wrote its argv"
        )
        argv = log.read_text(encoding="utf-8", errors="replace")
        assert minted not in argv
        assert "--env-file" in argv
        assert str(assistant.env_file_path()) in argv


def test_a_rotation_replaces_the_credential(config, long_lived):
    cfg.ensure_secret(config)
    with started(config):
        first = cfg.active_assistant_credential()
        rotated = assistant.rotate(config)
        try:
            second = cfg.active_assistant_credential()
            assert second and second != first, (
                "a rotation is a new incarnation and gets a new credential"
            )
        finally:
            kill(rotated.pid)


def test_a_stop_revokes_the_credential(config, long_lived):
    cfg.ensure_secret(config)
    with started(config):
        assert cfg.active_assistant_credential()
    assistant.stop()
    assert cfg.active_assistant_credential() is None
    assert not cfg.assistant_credential_path().exists()


def test_a_mint_that_fails_falls_back_to_the_shared_secret(
    config, long_lived, monkeypatch, caplog
):
    """A bookkeeping failure must not cost the operator their chat window."""
    shared = cfg.ensure_secret(config)

    def refuse():
        raise cfg.ConfigError("disk is full")

    monkeypatch.setattr(assistant, "mint_assistant_credential", refuse)
    with caplog.at_level("ERROR", logger="lmer_platform.assistant"):
        with started(config):
            assert env_values()[assistant.ENV_PLATFORM_CREDENTIAL] == shared
    assert any(
        "platform_assistant_credential_unmintable" in record.message
        for record in caplog.records
    ), "the fallback has to say why check-ins will not register"


# --- the guard tells the two apart -------------------------------------------

def test_both_credentials_open_the_api_and_are_told_apart(config, long_lived):
    """An identity, not a scope: both work, and the platform knows which called."""
    cfg.ensure_secret(config)
    client = client_for(config)
    with started(config):
        minted = cfg.active_assistant_credential()
        assert client.get("/api/state", headers=bearer(SECRET)).status_code == 200
        assert client.get("/api/state", headers=bearer(minted)).status_code == 200
        assert client.get("/api/state", headers=bearer("neither")).status_code == 401


def _guard_of(app):
    """The app's ``require_secret``, reachable for a direct call.

    Read off a route's dependant rather than re-derived, so the thing under test
    is the function the served routes actually gate on.
    """
    for route in app.routes:
        for dependency in getattr(route, "dependencies", ()):
            call = dependency.dependency
            if getattr(call, "__name__", "") == "require_secret":
                return call
    raise AssertionError("no require_secret dependency on any route")


def test_the_minted_credential_is_attributed_to_the_assistant(config, long_lived):
    cfg.ensure_secret(config)
    app = api.create_app(
        config, SECRET, state_builder=lambda config, force_pull=False: {}
    )
    guard = _guard_of(app)
    with started(config):
        minted = cfg.active_assistant_credential()
        caller = guard(f"Bearer {minted}")
        assert caller.kind == api.CALLER_ASSISTANT
        assert caller.is_assistant
    operator = guard(f"Bearer {SECRET}")
    assert operator.kind == api.CALLER_OPERATOR
    assert not operator.is_assistant


def test_a_revoked_credential_stops_authenticating(config, long_lived):
    cfg.ensure_secret(config)
    client = client_for(config)
    with started(config):
        minted = cfg.active_assistant_credential()
    assistant.stop()
    assert client.get("/api/state", headers=bearer(minted)).status_code == 401


# --- it does not survive a transcript ----------------------------------------

def test_the_scrub_strikes_the_minted_credential_by_value(config, long_lived):
    """While the incarnation is live, which is the window that matters.

    The read path is what serves a transcript to a browser, and it resolves the
    credential on every string — so an incarnation that ran ``env`` has the value
    masked in the chat view for as long as it is the live one.
    """
    cfg.ensure_secret(config)
    with started(config):
        minted = cfg.active_assistant_credential()
        text = f"I ran: curl -H 'x' {minted} and it worked"
        scrubbed = transcripts._scrub(text)
        assert minted not in scrubbed
        assert "<redacted>" in scrubbed


def test_the_scrub_cannot_reach_a_revoked_credential_and_says_so(
    config, long_lived
):
    """The boundary of the claim above, pinned rather than left to be assumed.

    ``stop()`` revokes before the container is gone and the exit-time file scrub
    runs after ``process.wait()``, so by then there is no value to strike: what a
    transcript can still hold is the **revoked** token, which opens nothing. The
    shared secret has no such gap — it is still on disk — and that difference is
    the reason this is written down.
    """
    cfg.ensure_secret(config)
    with started(config):
        minted = cfg.active_assistant_credential()
    assistant.stop()

    assert cfg.active_assistant_credential() is None
    assert minted in transcripts._scrub(f"the token was {minted}"), (
        "nothing to mask by value once it is revoked — the protection is that "
        "the value no longer authenticates, not that it is unfindable"
    )
    client = client_for(config)
    assert client.get("/api/state", headers=bearer(minted)).status_code == 401
