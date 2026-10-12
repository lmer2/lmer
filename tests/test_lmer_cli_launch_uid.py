#!/usr/bin/env python3
"""``--launch-uid``: the host mints an execution uid, the container records it.

The flag is the ONLY source. An inherited ``LMER_LAUNCH_UID`` — an agent that
runs ``lmer`` to spawn a child, or a stale ``.env`` — must never be forwarded:
the whole point of a pre-minted uid is that exactly one caller owns it, and a
fallback would hand every child launch the same identity.
"""

import os

import pytest

from lmer_cli.cli import _continuation_env, _validate_parsed_args, parse_args

UID = "0123456789abcdef0123456789abcdef"


def _parse(*extra):
    ns, _ = parse_args(["develop", "https://example.com/x/y", *extra])
    return ns


def test_launch_uid_is_exported_from_the_flag():
    ns = _parse("--launch-uid", UID)
    assert _validate_parsed_args(ns) is None
    assert _continuation_env(ns)["LMER_LAUNCH_UID"] == UID


def test_absent_flag_exports_none():
    ns = _parse()
    assert _validate_parsed_args(ns) is None
    assert _continuation_env(ns)["LMER_LAUNCH_UID"] is None


def test_ambient_launch_uid_is_never_forwarded(monkeypatch):
    monkeypatch.setenv("LMER_LAUNCH_UID", UID)
    ns = _parse()
    assert _validate_parsed_args(ns) is None
    assert _continuation_env(ns)["LMER_LAUNCH_UID"] is None


@pytest.mark.parametrize("bad", [
    "", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "0123456789abcdef0123456789abcdeZ",
    "not-a-uid", "0123456789abcdef0123456789abcd-",
])
def test_malformed_launch_uid_is_refused(bad, capsys):
    ns = _parse("--launch-uid", bad)
    assert _validate_parsed_args(ns) == 2
    assert "--launch-uid" in capsys.readouterr().out


def test_launch_uid_is_refused_with_session(capsys):
    ns = _parse("--session", UID, "--launch-uid", UID)
    assert _validate_parsed_args(ns) == 2
    assert "--session" in capsys.readouterr().out


def test_launch_uid_is_allowed_with_source_session():
    """A schema-2 continuation mints a new uid beside its read-only source."""
    ns = _parse("--source-session", "a" * 32, "--launch-uid", UID)
    assert _validate_parsed_args(ns) is None
    assert _continuation_env(ns)["LMER_LAUNCH_UID"] == UID
