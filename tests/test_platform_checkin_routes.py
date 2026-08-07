"""Which API calls count as the assistant checking on a run (issue #244).

:mod:`lmer_platform.checkin` owns what staleness *means*; this owns where the stamp
comes from. Two rules, each of which fails silently when it is wrong:

- **Only the assistant's credential counts.** The browser polls ``/messages`` and
  ``/ask`` every five seconds, so if the operator's reads stamped the run, one tab
  left open would suppress its reminders for as long as it stayed open.
- **Only routes naming one run stamp.** ``GET /api/state`` carries the whole fleet;
  counting it would mark everything checked every time the assistant looked at the
  list.

The rest is what those two need to mean anything: a session with no run identity
stamps nothing, a refused call stamps nothing, and the run a session belongs to is
read rather than guessed.
"""

import os

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, checkin, registry, runs, session_io, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-shared-secret-value"
RUN = ("git.example.com", "acme/widgets", "develop-issue-1")


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_REPO_URL", cfg.ENV_BIND_ADDRESS,
                 cfg.ENV_BIND_PORT, cfg.ENV_CONTAINER_URL, cfg.ENV_SECRET_FILE,
                 cfg.ENV_CHECKIN_WINDOW):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def client(config):
    return TestClient(
        api.create_app(
            config, SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )


@pytest.fixture
def assistant_credential(platform_root):
    """A credential that authenticates as the assistant, with no container.

    The minting is :mod:`lmer_platform.config`'s and is tested where it lives;
    what this file needs is a request that *arrives* as the assistant, which is
    the file's presence and nothing else.
    """
    return cfg.mint_assistant_credential()


@pytest.fixture
def session(platform_root):
    """A registered session belonging to :data:`RUN`, with a readable log."""
    host, project, slug = RUN
    log = store.logs_dir() / "s-checkin.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello from the session\n", encoding="utf-8")
    registry.register(
        "s-checkin",
        pid=os.getpid(),
        log_path=str(log),
        run={"host": host, "project": project, "slug": slug},
    )
    return "s-checkin"


def as_assistant(credential):
    return {"Authorization": f"Bearer {credential}"}


def as_operator():
    return {"Authorization": f"Bearer {SECRET}"}


def checked_at(run=RUN):
    marks = checkin.read_marks().get("/".join(run)) or {}
    return marks.get("checked_at")


# --- who is asking -----------------------------------------------------------

def test_the_assistant_reading_a_session_checks_its_run(
    client, session, assistant_credential
):
    reply = client.get(
        f"/api/sessions/{session}/messages", headers=as_assistant(assistant_credential)
    )
    assert reply.status_code == 200, reply.text
    assert checked_at(), "an assistant read of a run is a check-in"


def test_the_operators_own_read_does_not_check_anything(client, session):
    """The browser polls this route every five seconds with the shared secret."""
    for _ in range(3):
        assert client.get(
            f"/api/sessions/{session}/messages", headers=as_operator()
        ).status_code == 200
    assert checked_at() is None, (
        "a tab left open on a run must not suppress that run's reminders"
    )


def test_the_fleet_view_checks_nothing(client, session, assistant_credential):
    """Counting /api/state would mark the whole fleet checked at once."""
    assert client.get(
        "/api/state", headers=as_assistant(assistant_credential)
    ).status_code == 200
    assert checked_at() is None


# --- which routes ------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/sessions/{id}/messages"),
    ("GET", "/api/sessions/{id}/log"),
    ("GET", "/api/sessions/{id}/ask"),
    ("POST", "/api/sessions/{id}/tty-ticket"),
])
def test_every_session_read_the_assistant_makes_is_a_check(
    client, session, assistant_credential, method, path
):
    reply = client.request(
        method, path.format(id=session), headers=as_assistant(assistant_credential)
    )
    assert reply.status_code == 200, f"{method} {path}: {reply.text}"
    assert checked_at(), f"{method} {path} did not record a check-in"


def test_typing_into_a_session_is_a_check(
    client, session, assistant_credential, monkeypatch
):
    monkeypatch.setattr(
        session_io, "send_input", lambda *_a, **_k: {"bytes_written": 4}
    )
    reply = client.post(
        f"/api/sessions/{session}/input",
        headers=as_assistant(assistant_credential),
        json={"data": "/followup"},
    )
    assert reply.status_code == 200, reply.text
    assert checked_at()


def test_reading_a_runs_files_is_a_check(client, platform_root, assistant_credential):
    host, project, slug = RUN
    reply = client.get(
        "/api/runs/files",
        headers=as_assistant(assistant_credential),
        params={"host": host, "project": project, "slug": slug},
    )
    assert reply.status_code == 200, reply.text
    assert checked_at()


def test_bookkeeping_routes_are_not_check_ins(
    client, platform_root, assistant_credential
):
    """A title is a note *about* a run, not a look at it."""
    host, project, slug = RUN
    runs.track(host, project, slug)
    reply = client.post(
        "/api/runs/meta",
        headers=as_assistant(assistant_credential),
        json={"host": host, "project": project, "slug": slug, "title": "a name"},
    )
    assert reply.status_code == 200, reply.text
    assert checked_at() is None


# --- what is not a check -----------------------------------------------------

def test_a_refused_call_checks_nothing(client, platform_root, assistant_credential):
    reply = client.get(
        "/api/sessions/s-does-not-exist/messages",
        headers=as_assistant(assistant_credential),
    )
    assert reply.status_code == 404
    assert checkin.read_marks() == {}


def test_a_session_with_no_run_identity_stamps_nothing(
    client, platform_root, assistant_credential
):
    """A session spawned seconds ago, before its first ``work commit``."""
    log = store.logs_dir() / "s-unidentified.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("starting\n", encoding="utf-8")
    registry.register("s-unidentified", pid=os.getpid(), log_path=str(log))

    reply = client.get(
        "/api/sessions/s-unidentified/log",
        headers=as_assistant(assistant_credential),
    )
    assert reply.status_code == 200, reply.text
    assert checkin.read_marks() == {}


def test_an_unwritable_marks_file_does_not_break_the_read(
    client, session, assistant_credential, monkeypatch, caplog
):
    """The read happened; only the record of it did not."""
    def refuse(*_args, **_kwargs):
        raise store.StoreError("read-only filesystem")

    monkeypatch.setattr(checkin, "_stamp", refuse)
    with caplog.at_level("WARNING", logger="lmer_platform.checkin"):
        reply = client.get(
            f"/api/sessions/{session}/messages",
            headers=as_assistant(assistant_credential),
        )
    assert reply.status_code == 200, reply.text
    assert any(
        "platform_checkin_unrecorded" in record.message for record in caplog.records
    )


def test_the_stamp_moves_forward_on_a_second_read(
    client, session, assistant_credential, monkeypatch
):
    client.get(
        f"/api/sessions/{session}/messages", headers=as_assistant(assistant_credential)
    )
    first = checked_at()
    assert first

    stamps = iter(["2031-01-01T00:00:00Z"])
    monkeypatch.setattr(checkin, "utc_now_iso", lambda: next(stamps))
    client.get(
        f"/api/sessions/{session}/log", headers=as_assistant(assistant_credential)
    )
    assert checked_at() == "2031-01-01T00:00:00Z"


# --- acting on a run, not just reading it ------------------------------------
#
# The two run-keyed spawns take their identity from the request body rather than
# from a registry entry, so they are the other half of the stamping surface. The
# spawn itself is stubbed: what is under test is which run the stamp names, not
# whether a container starts.

class _Started:
    def to_dict(self):
        return {"session_id": "s-new"}


def test_answering_a_run_is_a_check(client, platform_root, assistant_credential,
                                    monkeypatch):
    monkeypatch.setattr(api, "answer_run", lambda *_a, **_k: _Started())
    host, project, slug = RUN
    reply = client.post(
        "/api/runs/answer",
        headers=as_assistant(assistant_credential),
        json={"host": host, "project": project, "slug": slug, "answer": "yes"},
    )
    assert reply.status_code == 202, reply.text
    assert checked_at()


def test_resuming_a_run_is_a_check(client, platform_root, assistant_credential,
                                   monkeypatch):
    monkeypatch.setattr(api, "resume_run", lambda *_a, **_k: _Started())
    host, project, slug = RUN
    reply = client.post(
        "/api/runs/resume",
        headers=as_assistant(assistant_credential),
        json={"host": host, "project": project, "slug": slug},
    )
    assert reply.status_code == 202, reply.text
    assert checked_at()


# --- the run shape #244 was actually filed about (review iteration 1) ----------
#
# A session that exits cleanly has its registry entry removed (spawn._watch on
# code == 0) while /messages, /log and /ask deliberately keep serving it from the
# PTY log — that pairing is what session_io.require_session exists for. Resolving
# the run from the registry entry alone therefore stamped nothing for a *dormant*
# run, which is the exact shape the issue was filed about: the assistant read it,
# got a 200, and the digest named it again every window forever.

def gone_session(session_id="s-gone", run=RUN):
    """A run whose session exited cleanly: tracked, with no registry entry."""
    runs.track(*run, session_id=session_id)
    log = store.logs_dir() / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("it said things and exited\n", encoding="utf-8")
    assert registry.read_session(session_id) is None, "precondition: no entry"
    return session_id


@pytest.mark.parametrize("path", [
    "/api/sessions/{id}/messages",
    "/api/sessions/{id}/log",
])
def test_reading_a_run_whose_session_exited_cleanly_checks_it(
    client, platform_root, assistant_credential, path
):
    session_id = gone_session()
    reply = client.get(
        path.format(id=session_id), headers=as_assistant(assistant_credential)
    )
    assert reply.status_code == 200, reply.text
    assert checked_at(), (
        "the assistant read the run the digest named and nothing was recorded — "
        "it would be announced again every window forever"
    )


def test_the_stamping_surface_matches_the_serving_surface(
    client, platform_root, assistant_credential
):
    """One resolver for both, so the two cannot drift apart again.

    ``sessions_for_run`` had the fallback and the stamping did not; both now ask
    ``runs.run_for_session``, and a session it cannot place stamps nothing rather
    than guessing.
    """
    session_id = gone_session()
    assert runs.run_for_session(session_id) == RUN
    assert runs.run_for_session("s-never-existed") is None

    reply = client.get(
        "/api/sessions/s-never-existed/messages",
        headers=as_assistant(assistant_credential),
    )
    assert reply.status_code == 404, "a session with neither entry nor log"
    assert checkin.read_marks() == {}
