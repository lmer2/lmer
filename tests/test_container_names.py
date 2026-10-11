"""Session containers get a human-readable ``--name`` (#444): the format, the
runtime charset, the length cap, and a create retried once when the name is
taken. Conflicts run against tests/fake_container_runtime.py, which refuses a
taken name the way each runtime does."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli import container_follow, runtime
from lmer_cli.runtime import NAME_MAX, NAME_RANDOM_HEX, container_name

FAKE = Path(__file__).with_name("fake_container_runtime.py")
CHARSET = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SESSION = "s-20261009T152516-ff2b"
RANDOM_SUFFIX = re.compile(rf"-[0-9a-f]{{{NAME_RANDOM_HEX}}}$")


def test_a_platform_session_is_named_by_its_slug_and_session_hex():
    assert container_name("chat-mr-389", SESSION) == "lmer-chat-mr-389-ff2b"


def test_a_cli_run_gets_a_random_suffix():
    first, second = container_name("develop-issue-444"), container_name("develop-issue-444")
    assert first.startswith("lmer-develop-issue-444-")
    assert RANDOM_SUFFIX.search(first) and RANDOM_SUFFIX.search(second)
    assert first != second, "a rerun of the same run must not reuse the name"


def test_without_a_slug_the_session_id_or_the_suffix_is_the_name():
    assert container_name(None, SESSION) == f"lmer-{SESSION}"
    assert re.fullmatch(rf"lmer-[0-9a-f]{{{NAME_RANDOM_HEX}}}", container_name(""))


def test_a_session_id_without_trailing_hex_falls_back_to_random():
    assert RANDOM_SUFFIX.search(container_name("develop", "s-20261009T152516"))


def test_a_retry_always_draws_a_fresh_random_suffix():
    retried = container_name("chat-mr-389", SESSION, retry=True)
    assert retried.startswith("lmer-chat-mr-389-") and not retried.endswith("-ff2b")
    assert RANDOM_SUFFIX.search(retried)
    no_slug = container_name(None, SESSION, retry=True)
    assert no_slug.startswith(f"lmer-{SESSION}-") and RANDOM_SUFFIX.search(no_slug)


@pytest.mark.parametrize("slug", [
    "feature/add dark-mode", "issue#12@gitlab", "--leading", "trailing..", "ünïcödé",
    "a//b  c", ".",
])
def test_names_stay_in_the_runtime_charset(slug):
    name = container_name(slug, SESSION)
    assert CHARSET.match(name), name
    assert "--" not in name and name.endswith("-ff2b")


def test_a_long_slug_is_cut_and_the_suffix_never_is():
    name = container_name("x" * 200, SESSION)
    assert len(name) == NAME_MAX and name.endswith("-ff2b")
    cli = container_name("develop-" + "y" * 200)
    assert len(cli) == NAME_MAX and RANDOM_SUFFIX.search(cli)


def test_a_long_session_hex_is_capped_so_the_name_cap_always_holds():
    session = "s-20261009T152516-" + "0123456789abcdef" * 8
    name = container_name("x" * 200, session)
    assert len(name) <= NAME_MAX and name.endswith("-" + session[-12:])
    assert len(container_name(None, session)) <= NAME_MAX
    assert len(container_name("", "a" * 300)) <= NAME_MAX


def test_a_cut_never_leaves_a_separator_before_the_suffix():
    room = NAME_MAX - len("lmer-") - len("-ff2b")
    name = container_name("a" * (room - 1) + "-tail", SESSION)
    assert "--" not in name and name.endswith("a-ff2b")


# --- create, against the fake runtime -----------------------------------------


@pytest.fixture(params=["docker", "podman"])
def fake(request, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / request.param
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RUNTIME_STATE", str(tmp_path / "containers"))
    monkeypatch.setenv("FAKE_RUNTIME_FLAVOR", request.param)
    return request.param


def _args(rt, name, cid_file):
    return [rt, "create", "--label", "io.lmer.managed=1", "--name", name,
            "--cidfile", str(cid_file),
            "fake-image", "/bin/sh", "-c", "exit 0"]


def _create_named(rt, name, tmp_path, tag):
    cid_file = tmp_path / f"{tag}.cid"
    return subprocess.run(_args(rt, name, cid_file), capture_output=True, text=True), cid_file


def test_the_fake_refuses_a_taken_name_as_the_runtimes_do(fake, tmp_path):
    first, _ = _create_named(fake, "lmer-x-1", tmp_path, "a")
    assert first.returncode == 0
    second, cid_file = _create_named(fake, "lmer-x-1", tmp_path, "b")
    assert second.returncode == 125
    assert "already in use" in second.stderr
    assert not cid_file.exists()
    assert container_follow.name_taken(fake, "lmer-x-1")
    assert not container_follow.name_taken(fake, "lmer-x-2")


def test_a_taken_name_is_retried_once_under_a_fresh_one(fake, tmp_path, capsys):
    _create_named(fake, "lmer-run-ff2b", tmp_path, "old")
    cid_file = tmp_path / "new.cid"

    rc, cid, name = container_follow.create(
        fake, _args(fake, "lmer-run-ff2b", cid_file), cid_file,
        rename=lambda: "lmer-run-abc123",
    )

    assert (rc, name) == (0, "lmer-run-abc123")
    assert cid and cid == cid_file.read_text()
    assert "lmer-run-ff2b is taken; creating lmer-run-abc123" in capsys.readouterr().err


def test_a_second_conflict_is_not_retried_again(fake, tmp_path):
    _create_named(fake, "lmer-a", tmp_path, "a")
    _create_named(fake, "lmer-b", tmp_path, "b")
    cid_file = tmp_path / "new.cid"
    renames = iter(["lmer-b", "lmer-c"])

    rc, cid, name = container_follow.create(
        fake, _args(fake, "lmer-a", cid_file), cid_file, rename=lambda: next(renames),
    )

    assert (rc, cid, name) == (125, "", "lmer-b")


def test_a_failure_that_is_not_a_name_conflict_is_not_retried(fake, tmp_path):
    cid_file = tmp_path / "stale.cid"
    cid_file.write_text("left over")
    renamed = []

    rc, _, name = container_follow.create(
        fake, _args(fake, "lmer-free", cid_file), cid_file,
        rename=lambda: renamed.append(1) or "lmer-other",
    )

    assert rc == 125 and name == "lmer-free" and renamed == []


def test_the_retry_name_comes_from_container_name(fake, tmp_path):
    taken = container_name("develop-issue-444", SESSION)
    _create_named(fake, taken, tmp_path, "old")
    cid_file = tmp_path / "new.cid"

    rc, _, name = container_follow.create(
        fake, _args(fake, taken, cid_file), cid_file,
        rename=lambda: runtime.container_name("develop-issue-444", SESSION, retry=True),
    )

    assert rc == 0 and name != taken and name.startswith("lmer-develop-issue-444-")


def test_following_by_name_keeps_the_whole_name_in_the_notice(fake, tmp_path, monkeypatch):
    """Names match exactly, so `lmer follow lmer-chat-63` would follow nothing."""
    _create_named(fake, "lmer-chat-mr-389-ff2b", tmp_path, "c")
    monkeypatch.setenv(runtime.RUNTIME_ENV, fake)
    seen = {}
    monkeypatch.setattr(container_follow, "follow",
                        lambda rt, container, **kw: seen.update(kw, container=container) or 0)

    assert container_follow.main(["lmer-chat-mr-389-ff2b"]) == 0
    assert seen["container"] == "lmer-chat-mr-389-ff2b"
    assert seen["name"] == "lmer-chat-mr-389-ff2b"

    container_follow.main(["0123456789abcdef0123"])
    assert seen["name"] is None, "an id is still shortened in the notice"
