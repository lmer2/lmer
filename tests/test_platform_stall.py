"""The operator signal when the assistant is not taking its spool (issue #584).

The detector's side — when it fires, once per episode — is pinned in
``tests/test_platform_nudge.py``. This suite pins what the signal *says* and
where it goes: a named marker rather than terminal text or a diagnosis, and a
post that a connector watching the thread will not type back into the stuck
assistant.
"""

import pytest

from lmer_cli.threads import ThreadError
from lmer_platform import session_io, stall


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    log = tmp_path / "session.log"

    def _write(data: bytes):
        log.write_bytes(data)
        monkeypatch.setattr(stall.session_io, "canonical_log",
                            lambda session_id: (log, session_io.LOG_SOURCE_HOST))
        return log

    return _write


def test_the_reason_is_the_marker_the_terminal_drew(terminal):
    terminal(
        b"\x1b[2J\x1b[H\x1b[38;5;246mWelcome back\x1b[39m\r\n"
        b"\x1b[31mLogin\x1b[1Cexpired \xc2\xb7 run something else\x1b[39m\r\n"
        b"\x1b[?25h> \r\n"
    )

    assert stall.terminal_reason("assistant-1") == "Login expired"


def test_an_api_refusal_names_only_its_marker(terminal):
    terminal(b'API Error: 401 {"type":"error","error":{"type":"authentication_failed"}}\n')

    assert stall.terminal_reason("assistant-1") == "authentication_failed"


def test_the_latest_marker_wins(terminal):
    terminal(b"login expired (old)\nworking\nrate_limit_error: slow down\n")

    assert stall.terminal_reason("assistant-1") == "rate_limit"


def test_a_terminal_without_a_marker_gives_no_reason(terminal):
    terminal(b"\x1b[1mthinking...\x1b[0m\n> \n")

    assert stall.terminal_reason("assistant-1") is None


def test_only_the_tail_is_searched(terminal):
    terminal(b"Login expired\n" + b"x" * (stall.TAIL_BYTES + 10) + b"\n")

    assert stall.terminal_reason("assistant-1") is None


def test_a_redraw_never_glues_other_rows_into_the_reason(terminal):
    rows = b"".join(b"\x1b[%d;1Hoperator: draft the reply to the customer row %d" % (n, n)
                    for n in range(1, 6))
    terminal(rows + b"\x1b[40;1HLogin expired")

    assert stall.terminal_reason("assistant-1") == "Login expired"


def test_words_on_two_rows_are_not_a_marker(terminal):
    terminal(b"\x1b[3;70HLogin\x1b[4;1Hexpired tasks: 0\n")

    assert stall.terminal_reason("assistant-1") is None


def test_a_marker_deep_in_a_long_line_is_still_named(terminal):
    terminal(b'{"detail":"' + b"x" * 250 + b'","type":"authentication_failed"}\n')

    assert stall.terminal_reason("assistant-1") == "authentication_failed"


def test_a_credential_cut_at_the_window_edge_never_reaches_the_reason(terminal):
    line = b"pre glpat-" + b"Q" * 30 + b" authentication_failed"
    filler = b"\n" + b"y" * (stall.TAIL_BYTES - (len(line) - 7) - 1)
    terminal(line + filler)

    assert stall.terminal_reason("assistant-1") == "authentication_failed"


def test_the_message_names_the_marker_and_does_not_diagnose():
    text = stall.message(project="lmer", pending=12, waited_minutes=17, nudges=3,
                         reason="Login expired")

    assert text.startswith("The lmer assistant is not taking its spool: 12 digests")
    assert "Its terminal shows `Login expired`." in text
    assert "logged out" not in text
    assert "\n" not in text


def test_the_message_says_so_when_no_marker_was_seen():
    text = stall.message(project=None, pending=1, waited_minutes=1, nudges=3, reason=None)

    assert text.startswith("The assistant is not taking its spool: 1 digest has waited 1 minute")
    assert "saw no known reason" in text


def test_delivery_posts_unattributed_so_no_connector_relays_it(monkeypatch):
    calls = []

    def post_thread(permalink, text, *, label, **kwargs):
        calls.append((permalink, text, label, kwargs))
        if "bad" in permalink:
            raise ThreadError("channel_not_found", status=404)
        return {"posted": {}}

    monkeypatch.setattr(stall.thread_access, "post_thread", post_thread)

    posted, failed = stall.deliver("hello", ["https://good/1", "https://bad/2"])

    assert posted == ["https://good/1"]
    assert failed == [{"thread": "https://bad/2", "error": "channel_not_found"}]
    assert all(label == "lmer platform" and "poster" not in kwargs
               for _p, _t, label, kwargs in calls)


def test_any_failure_on_one_thread_leaves_the_others_posted(monkeypatch):
    def post_thread(permalink, text, *, label, **kwargs):
        if "bad" in permalink:
            raise RuntimeError("socket closed")
        return {"posted": {}}

    monkeypatch.setattr(stall.thread_access, "post_thread", post_thread)

    posted, failed = stall.deliver("hello", ["https://bad/1", "https://good/2"])

    assert posted == ["https://good/2"]
    assert failed == [{"thread": "https://bad/1", "error": "socket closed"}]


def test_a_failed_posts_error_is_scrubbed_before_it_is_stored(monkeypatch):
    secret = "glpat-abcdefghijklmnopqrstu"

    def post_thread(permalink, text, *, label, **kwargs):
        raise RuntimeError(f"request Authorization: Bearer {secret} refused " + "z" * 400)

    monkeypatch.setattr(stall.thread_access, "post_thread", post_thread)

    _posted, failed = stall.deliver("hello", ["https://bad/1"])

    assert secret not in failed[0]["error"]
    assert failed[0]["error"].startswith("request Authorization: ")
    assert len(failed[0]["error"]) <= 300
