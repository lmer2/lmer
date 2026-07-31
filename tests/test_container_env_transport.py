"""Container env transport: no secret value in argv (issue #158).

Contract under test
-------------------
``runtime.build_container_env()`` carries a session's environment into its
container without putting any value in the ``docker``/``podman run`` argv,
where ``/proc/<pid>/cmdline`` — world-readable — exposed every token a
session carries to any user on the host for the lifetime of the spawn.

Two legs, and the tests below pin both:

- Values the line-oriented env-file format can carry go into a mode-0600
  file inside a mode-0700 directory, passed as ``--env-file``.
- Everything else rides a bare ``-e NAME`` inheritance marker with the value
  handed to the runtime client's own environment. Overwhelmingly this is a
  value containing ``\\n``/``\\r``, which is not hypothetical: ``--prompt``,
  ``--answer`` and Slack thread text carry newlines today, and reached argv
  intact before this change.

There is no third leg. A variable neither transport can carry raises
``ContainerEnvError`` and aborts the launch — the inline ``-e NAME=value``
fallback the first iteration shipped was reachable with a credential from a
``.env`` file, which is exactly the exposure this module exists to close.

What these tests deliberately do NOT prove: that docker's and podman's
env-file *parsers* agree on quoting and ``$`` handling. Nothing promises
they do, and it cannot be established from the host side — so the assertions
here are over the exact bytes lmer writes, and the cross-runtime half lives
in ``test_container_env_transport_integration.py``, which round-trips the
same hazard set through every runtime actually present.
"""

import io
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from lmer_cli.mounts import build_user_mounts as _real_build_user_mounts
from lmer_cli.runtime import (
    ContainerEnv,
    ContainerEnvError,
    _client_reads_name,
    _ENV_FILE_MAX_LINE_BYTES,
    _ENV_FILE_NAME,
    build_container_env,
)
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"

from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)

# Values chosen to break a naive transport: shell metacharacters, both quote
# kinds, an embedded '=', a leading '#', significant surrounding whitespace,
# an empty value and non-ASCII. Every one of these is representable in an
# env-file line, so all of them must round-trip byte-for-byte.
_HAZARD_VALUES = {
    "LMER_HAZARD_SPACES": "value with spaces",
    "LMER_HAZARD_DQUOTE": 'he said "hello"',
    "LMER_HAZARD_SQUOTE": "it's fine",
    "LMER_HAZARD_BOTH_QUOTES": "\"quoted\" and 'quoted'",
    "LMER_HAZARD_EQUALS": "a=b=c",
    "LMER_HAZARD_DOLLAR": "$HOME and ${NOT_EXPANDED}",
    "LMER_HAZARD_HASH": "value # not a comment",
    "LMER_HAZARD_LEADING_WS": "   leading",
    "LMER_HAZARD_TRAILING_WS": "trailing   ",
    "LMER_HAZARD_TAB": "a\tb",
    "LMER_HAZARD_BACKSLASH": r"back\slash and \n which is not a newline",
    "LMER_HAZARD_EMPTY": "",
    "LMER_HAZARD_UNICODE": "héllo → wörld",
}

# Distinctive enough that finding one in argv can only mean a transport leak.
_SENTINEL_WORK_REPO = (
    "https://oauth2:glpat-sentinel-workrepo-158@example.com/agents/work.git"
)
_SENTINEL_FASTAPI = "sentinel-fastapi-token-158"
_SENTINEL_EFFORT = "sentinel-reasoning-effort-158"

_NAME_EQUALS_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _read_env_file(container_env):
    """The env file's raw text, or None when no file was written."""
    if container_env.env_file_dir is None:
        return None
    return (container_env.env_file_dir / _ENV_FILE_NAME).read_text(encoding="utf-8")


def _parsed_env_file(container_env):
    """The env file parsed the way docker and podman parse it: split on the
    first ``=``, value taken verbatim."""
    text = _read_env_file(container_env)
    if text is None:
        return {}
    parsed = {}
    for line in text.split("\n"):
        if not line:
            continue
        name, _, value = line.partition("=")
        parsed[name] = value
    return parsed


class TestEnvFileLeg:
    """Representable values ride a private --env-file, never argv."""

    def test_hazard_values_round_trip_byte_for_byte(self):
        """Every hazard value survives the transport exactly.

        This is the acceptance criterion 'values with spaces/quotes/= survive
        the transport intact' asserted against what lmer writes.
        """
        container_env = build_container_env(dict(_HAZARD_VALUES))
        try:
            parsed = _parsed_env_file(container_env)
            for name, value in _HAZARD_VALUES.items():
                assert parsed.get(name) == value, (
                    f"{name} did not survive the env file: "
                    f"wrote {parsed.get(name)!r}, expected {value!r}"
                )
        finally:
            container_env.cleanup()

    def test_no_hazard_value_appears_in_args(self):
        """None of the values leak into the argument list."""
        container_env = build_container_env(dict(_HAZARD_VALUES))
        try:
            joined = " ".join(container_env.args)
            for name, value in _HAZARD_VALUES.items():
                if not value:
                    continue
                assert value not in joined, (
                    f"{name}'s value leaked into argv: {joined!r}"
                )
        finally:
            container_env.cleanup()

    def test_env_file_flag_precedes_any_markers(self):
        """--env-file and its path are the first two args."""
        container_env = build_container_env({"FOO": "bar", "MULTI": "a\nb"})
        try:
            assert container_env.args[0] == "--env-file"
            assert Path(container_env.args[1]).is_file()
        finally:
            container_env.cleanup()

    def test_trailing_newline_terminates_every_line(self):
        """Each line is newline-terminated so the last variable is not dropped
        by a parser that requires a line terminator."""
        container_env = build_container_env({"A": "1", "B": "2"})
        try:
            assert _read_env_file(container_env) == "A=1\nB=2\n"
        finally:
            container_env.cleanup()

    def test_punctuated_name_is_an_ordinary_line(self):
        """A dashed/dotted key is a valid env-file line and docker accepts it.

        Only the shapes a parser would misread are diverted (see
        ``TestNamesAnEnvFileWouldMisread``); ordinary punctuation is not one of
        them, so this must not be filtered or rerouted.
        """
        container_env = build_container_env({"has-a-dash": "kept"})
        try:
            assert _read_env_file(container_env) == "has-a-dash=kept\n"
        finally:
            container_env.cleanup()

    def test_equals_in_the_name_keeps_its_historical_meaning(self):
        """``FOO=BAR`` as a key splits on the first ``=``, exactly as
        ``-e FOO=BAR=x`` always did — so it is an ordinary line, not a
        blocker."""
        container_env = build_container_env({"FOO=BAR": "x"})
        try:
            assert _read_env_file(container_env) == "FOO=BAR=x\n"
            assert _parsed_env_file(container_env) == {"FOO": "BAR=x"}
        finally:
            container_env.cleanup()


class TestNamesAnEnvFileWouldMisread:
    """A name a parser would drop, rename or reject never becomes a line.

    A leading ``#`` reads as a comment, an empty name is rejected, and docker
    aborts the spawn on whitespace in a name while podman accepts it — so
    writing any of these would drop the variable or make the outcome
    runtime-dependent. They route to the marker leg where the name allows it,
    and otherwise abort. Nothing falls back to argv.
    """

    def test_comment_shaped_name_rides_the_marker_leg(self):
        """``#notavar`` cannot be a line, but is a fine bare marker name."""
        container_env = build_container_env({"#notavar": "kept"})
        try:
            assert _read_env_file(container_env) is None
            assert container_env.args == ["-e", "#notavar"]
            assert container_env.client_env == {"#notavar": "kept"}
        finally:
            container_env.cleanup()

    def test_space_in_name_rides_the_marker_leg(self):
        """docker rejects ``variable 'M N' contains whitespaces`` on the FILE
        leg only — a bare ``-e "M N"`` resolves fine (probed against docker
        29.6.2, rc 0, ``Env: ['M N=line1\\nline2']``).

        So this is diverted, not refused. Iteration 2 corrected an over-block
        here: aborting would have refused a launch that worked before #158, for
        a key ``dotenv_values`` really produces (``'M N'=1`` → ``M N``).
        """
        container_env = build_container_env({"M N": "line1\nline2"})
        try:
            assert _read_env_file(container_env) is None
            assert container_env.args == ["-e", "M N"]
            assert container_env.client_env == {"M N": "line1\nline2"}
        finally:
            container_env.cleanup()

    def test_space_in_name_with_a_plain_value_also_diverts(self):
        """The name alone blocks the file leg, whatever the value is."""
        container_env = build_container_env({" PADDED ": "kept"})
        try:
            assert _read_env_file(container_env) is None
            assert container_env.args == ["-e", " PADDED "]
        finally:
            container_env.cleanup()

    def test_exotic_whitespace_in_a_name_is_not_blocked(self):
        """docker's check is ``ContainsAny(name, " \\t")``, not ``isspace()``.

        NBSP parses fine in docker's env-file parser, so treating it as a
        blocker would divert (or previously refuse) a name no runtime objects
        to. INTERIOR only \u2014 the leading case is a rename, tested below.
        """
        container_env = build_container_env({"FO\u00a0O": "kept"})
        try:
            assert _parsed_env_file(container_env) == {"FO\u00a0O": "kept"}
        finally:
            container_env.cleanup()

    @pytest.mark.parametrize(
        "name",
        [
            "\u00a0FOO",  # NBSP: the copy-paste artifact this is really about
            "\x0bFOO",  # VT
            "\x0cFOO",  # FF
            "\u3000FOO",  # ideographic space
            " FOO",
            "\tFOO",
        ],
    )
    def test_leading_whitespace_in_a_name_rides_the_marker_leg(self, name):
        """docker RENAMES these on the file leg; the marker leg preserves them.

        ``strings.TrimLeftFunc(line, unicode.IsSpace)`` runs before the split on
        ``=``, and Go's ``unicode.IsSpace`` covers NBSP/VT/FF/U+3000, so every
        name here arrives as plain ``FOO`` (probed against docker 29.6.2), while
        ``-e '\\xa0FOO'`` resolves with the name intact. A rename is the exact
        failure this blocker exists to prevent: ``Config.Env`` is last-wins, so
        a stray NBSP-prefixed copy of ``GITLAB_TOKEN`` would silently displace
        the real one.
        """
        container_env = build_container_env({name: "kept"})
        try:
            assert _read_env_file(container_env) is None, (
                f"{name!r} reached the env file, where docker strips the "
                f"leading whitespace and delivers it as {name.lstrip()!r}"
            )
            assert container_env.args == ["-e", name]
            assert container_env.client_env == {name: "kept"}
        finally:
            container_env.cleanup()

    def test_a_quoted_dotenv_key_really_produces_a_leading_nbsp_name(self, tmp_path):
        """Reachability, not theory, but only through a QUOTED key.

        A bare ``\\xa0BARE=x`` is normalised by the loader (the NBSP never
        reaches the builder); a quoted one survives verbatim.
        """
        env_file = tmp_path / ".env"
        env_file.write_text("'\u00a0QUOTEDNBSP'=typo\n\u00a0BARE=x\n")
        loaded = dotenv_values(env_file)
        assert "\u00a0QUOTEDNBSP" in loaded
        assert "BARE" in loaded and "\u00a0BARE" not in loaded

    def test_leading_dash_name_rides_the_marker_leg(self):
        """``-e -FOO`` resolves: pflag consumes the operand after ``-e``
        verbatim (probed, rc 0, ``Env: ['-FOO=dash-value']``), and
        ``dotenv_values`` produces ``-FOO`` from ``-FOO=…``."""
        container_env = build_container_env({"-FOO": "a\nb"})
        try:
            assert container_env.args == ["-e", "-FOO"]
            assert container_env.client_env == {"-FOO": "a\nb"}
        finally:
            container_env.cleanup()

    def test_empty_name_aborts(self):
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"": "kept"})
        assert "empty" in str(excinfo.value)

    def test_a_misreadable_name_does_not_suppress_the_good_ones(self):
        """Routing is per-variable; everything else still rides the file."""
        container_env = build_container_env({"#bad": "x", "GOOD": "y"})
        try:
            assert _parsed_env_file(container_env) == {"GOOD": "y"}
            assert container_env.client_env == {"#bad": "x"}
        finally:
            container_env.cleanup()


class TestNoInlineFallbackRemains:
    """The "no value in argv" invariant is enforced, not documented.

    Review of iteration 1 showed the old warn-and-inline fallback was
    reachable with a credential straight from a ``.env`` file, so acceptance
    criterion 1 did not hold. Two things changed: names with ordinary
    punctuation now ride the marker leg, and anything neither leg can carry
    aborts the launch.
    """

    def test_dotenv_multiline_private_key_never_reaches_argv(self, tmp_path):
        """The reported repro, driven through the real ``.env`` loader.

        ``app.private-key`` fails an identifier check (dot, dashes) and its
        value is multi-line, which used to land it inline — putting a private
        key in ``/proc/<pid>/cmdline``.
        """
        env_file = tmp_path / "probe.env"
        env_file.write_text(
            'app.private-key="-----BEGIN OPENSSH PRIVATE KEY-----\n'
            "b3BlbnNzaC1rZXktdjEAAAAA\n"
            '-----END OPENSSH PRIVATE KEY-----"\n'
        )
        loaded = dict(dotenv_values(dotenv_path=str(env_file)))
        assert "app.private-key" in loaded, "dotenv no longer yields this key"
        assert "\n" in loaded["app.private-key"]

        container_env = build_container_env(loaded)
        try:
            assert container_env.args == ["-e", "app.private-key"]
            assert (
                container_env.client_env["app.private-key"]
                == loaded["app.private-key"]
            )
            assert "BEGIN OPENSSH PRIVATE KEY" not in " ".join(container_env.args)
        finally:
            container_env.cleanup()

    def test_postcondition_holds_for_awkward_names(self):
        """Every ``-e`` the transport emits carries a bare name.

        Asserted inside ``build_container_env`` so the invariant cannot drift
        away from the code; this drives that check over names that previously
        would have been inlined.

        Walks the emitted PAIRS, like the postcondition itself does: an
        element-wise walk reading ``args[index + 1]`` runs off the end when a
        marker name is ``-e`` — the bug the ``-e`` entry below regresses.
        """
        for name in ("PLAIN", "app.private-key", "#hash", "wëird", "-e"):
            container_env = build_container_env({name: "value\nwith newline"})
            try:
                args = container_env.args
                for flag, operand in zip(args[::2], args[1::2]):
                    if flag == "-e":
                        assert "=" not in operand
            finally:
                container_env.cleanup()

    def test_a_marker_name_of_dash_e_does_not_crash_the_builder(self):
        """``dotenv_values('-e=…')`` really yields the key ``-e``.

        With a multi-line value it routes to the marker leg, so ``args`` ends
        ``['-e', '-e']`` — and the postcondition's old element-wise walk indexed
        past the end, raising ``IndexError`` out of the builder. ``cli.py``
        catches ``ContainerEnvError``, not ``IndexError``, so that surfaced as a
        traceback and rc 1 where the previous commit exited cleanly. No value is
        exposed either way; this is robustness, not leakage.

        Real docker is indifferent: ``-e -e`` consumes the operand and yields
        ``Env: ['-e=a\\nb']`` (probed in review).
        """
        loaded = dict(dotenv_values(stream=io.StringIO('-e="line1\nline2"')))
        assert "-e" in loaded, "dotenv no longer yields this key"

        container_env = build_container_env(loaded)
        try:
            assert container_env.args == ["-e", "-e"]
            assert container_env.client_env == {"-e": "line1\nline2"}
        finally:
            container_env.cleanup()

    def test_reserved_name_with_unrepresentable_value_aborts(self):
        """Corrupting the client's own HOME is not an option, and neither is
        argv — so it aborts."""
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"HOME": "/home/developer\nbogus"})
        message = str(excinfo.value)
        assert "HOME" in message
        assert "reads HOME from its own environment" in message

    def test_reserved_set_covers_the_names_the_clients_really_read(self):
        """Every name either client consumes is reserved, listed explicitly."""
        for name in (
            "PATH",
            "HOME",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_API_VERSION",
            "DOCKER_DEFAULT_PLATFORM",
            # Lost when iteration 2 reverted the DOCKER_ prefix to a list. The
            # client reads it: with DOCKER_TLS=1 set, docker never reaches the
            # create call ("unable to resolve docker endpoint: open
            # ~/.docker/ca.pem: no such file or directory", rc 1).
            "DOCKER_TLS",
            "CONTAINER_HOST",
            "CONTAINERS_CONF",
            "PODMAN_CONNECTIONS_CONF",
            "REGISTRY_AUTH_FILE",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "LD_PRELOAD",
        ):
            assert _client_reads_name(name), f"{name} must be treated as reserved"

    def test_reserved_set_does_not_over_reserve_by_prefix(self):
        """Under fail-closed routing this set gates a hard abort, so a blanket
        ``CONTAINER_*``/``XDG_*``/``LD_*`` prefix would refuse launches for
        names no client reads.

        ``CONTAINER_PAYLOAD`` with a multi-line value rode ``-e`` before #158;
        iteration 2 caught the prefix rewrite refusing it.
        """
        for name in (
            "CONTAINER_PAYLOAD",
            "REGISTRY_MANIFEST",
            "XDG_DATA_HOME",
            "LMER_WORK_REPO",
            "GITLAB_TOKEN",
        ):
            assert not _client_reads_name(name), f"{name} must NOT be reserved"

    def test_over_reserved_name_still_reaches_the_container(self):
        """The routing consequence of the previous test, end to end."""
        container_env = build_container_env({"CONTAINER_PAYLOAD": "a\nb"})
        try:
            assert container_env.args == ["-e", "CONTAINER_PAYLOAD"]
            assert container_env.client_env == {"CONTAINER_PAYLOAD": "a\nb"}
        finally:
            container_env.cleanup()

    def test_proxy_names_are_reserved_in_either_case(self):
        assert _client_reads_name("http_proxy")
        assert _client_reads_name("HTTPS_PROXY")
        assert _client_reads_name("all_proxy")


class TestEnvFileLineLimit:
    """A line at or over the parsers' 64 KiB token limit aborts the spawn.

    Verified in review against docker 29.6.2: a 70,000-char single-line value
    gives ``bufio.Scanner: token too long``, rc 125, and no container. Before
    #158 the same value worked as ``-e NAME=<70000 chars>``, so it must keep
    working — via the marker leg, whose ceiling (``MAX_ARG_STRLEN``, 128 KiB)
    is twice as generous.
    """

    def test_oversized_value_takes_the_marker_leg(self):
        value = "x" * 70000
        container_env = build_container_env({"BIG": value})
        try:
            assert container_env.args == ["-e", "BIG"]
            assert container_env.client_env == {"BIG": value}
            assert _read_env_file(container_env) is None
        finally:
            container_env.cleanup()

    def test_value_under_the_limit_still_rides_the_env_file(self):
        value = "x" * 60000
        container_env = build_container_env({"BIG": value})
        try:
            assert _parsed_env_file(container_env) == {"BIG": value}
        finally:
            container_env.cleanup()

    def test_boundary_is_measured_on_encoded_bytes(self):
        """A multibyte value must be counted the way the parser counts it.

        ``é`` is two UTF-8 bytes, so a value of half the limit in characters is
        over it in bytes — measuring ``len(str)`` would write a line docker
        rejects.
        """
        name = "MB"
        value = "é" * (_ENV_FILE_MAX_LINE_BYTES // 2)
        assert len(value) < _ENV_FILE_MAX_LINE_BYTES
        assert len(f"{name}={value}".encode("utf-8")) > _ENV_FILE_MAX_LINE_BYTES
        container_env = build_container_env({name: value})
        try:
            assert container_env.args == ["-e", name], (
                "a multibyte value over the byte limit must not be written"
            )
        finally:
            container_env.cleanup()

    def test_exactly_at_the_limit_is_treated_as_over(self):
        """The bound is inclusive: ``bufio.Scanner`` errors once a token
        reaches its buffer size, so the safe rule is ``>=``."""
        name = "EXACT"
        value = "x" * (_ENV_FILE_MAX_LINE_BYTES - len(name) - 1)
        assert len(f"{name}={value}".encode("utf-8")) == _ENV_FILE_MAX_LINE_BYTES
        container_env = build_container_env({name: value})
        try:
            assert container_env.args == ["-e", name]
        finally:
            container_env.cleanup()

    def test_one_byte_under_the_limit_rides_the_file(self):
        name = "UNDER"
        value = "x" * (_ENV_FILE_MAX_LINE_BYTES - len(name) - 2)
        container_env = build_container_env({name: value})
        try:
            assert _parsed_env_file(container_env) == {name: value}
        finally:
            container_env.cleanup()


class TestBuilderOwnsItsOwnFailures:
    """A failure between mkdtemp and return must not leak a directory.

    ``cli.py``'s finally can only clean a ``ContainerEnv`` it received, so
    anything raised inside the builder would otherwise strand a directory —
    on a partial write, one with token lines already in it.
    """

    def test_write_failure_leaks_no_file_descriptor(self, monkeypatch):
        """``os.fdopen`` raising leaves the descriptor from ``os.open`` ours.

        Without an explicit close that is one leaked fd per failure — and this
        is the path the failure test below actually takes.
        """
        def _boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fdopen", _boom)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            with pytest.raises(OSError):
                build_container_env({"GITLAB_TOKEN": "glpat-leak-demo"})
        after = len(os.listdir("/proc/self/fd"))
        assert after <= before, f"leaked descriptors: {before} -> {after}"

    def test_failure_inside_write_also_cleans_up(self, monkeypatch):
        """The case a real ENOSPC takes.

        ``os.fdopen`` succeeding and the failure surfacing from ``write``/
        ``close`` inside the ``with`` is what a full disk actually produces —
        the other test patches ``fdopen``, which is not that path.
        """
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        real_fdopen = os.fdopen

        def _failing_fdopen(*args, **kwargs):
            handle = real_fdopen(*args, **kwargs)

            def _boom(*_a, **_k):
                raise OSError(28, "No space left on device")

            handle.write = _boom
            return handle

        monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
        monkeypatch.setattr(os, "fdopen", _failing_fdopen)

        with pytest.raises(OSError):
            build_container_env({"GITLAB_TOKEN": "glpat-leak-demo"})
        assert created
        for directory in created:
            assert not directory.exists(), f"{directory} leaked on a write failure"

    def test_write_failure_leaves_no_directory_behind(self, monkeypatch):
        """The general case the surrogate repro stood in for: ENOSPC/EIO."""
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)

        def _boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fdopen", _boom)

        with pytest.raises(OSError):
            build_container_env({"GITLAB_TOKEN": "glpat-leak-demo"})
        assert created, "mkdtemp was never reached; the test no longer covers it"
        for directory in created:
            assert not directory.exists(), (
                f"{directory} leaked — the builder must clean up its own failures"
            )


class TestInheritanceLeg:
    """Values an env file cannot carry ride a bare -e NAME marker."""

    def test_newline_value_becomes_a_marker_and_client_env(self):
        """The value reaches the client environment, and only the NAME is argv."""
        prompt = "first line\nsecond line"
        container_env = build_container_env({"LMER_START_PROMPT": prompt})
        try:
            assert "-e" in container_env.args
            assert "LMER_START_PROMPT" in container_env.args
            assert container_env.client_env == {"LMER_START_PROMPT": prompt}
            assert prompt not in " ".join(container_env.args)
            # No NAME=value element at all: the marker is a bare name.
            assert not any(
                _NAME_EQUALS_VALUE_RE.match(arg) for arg in container_env.args
            )
        finally:
            container_env.cleanup()

    def test_newline_value_is_not_written_to_the_env_file(self):
        """A truncated line would silently corrupt the value — never write it."""
        container_env = build_container_env(
            {"SAFE": "ok", "LMER_ANSWER": "yes\nreally"}
        )
        try:
            parsed = _parsed_env_file(container_env)
            assert parsed == {"SAFE": "ok"}
        finally:
            container_env.cleanup()

    def test_carriage_return_also_takes_the_inheritance_leg(self):
        """A bare \\r would be swallowed by Go's line splitter, so it is
        unrepresentable too."""
        container_env = build_container_env({"LMER_ANSWER": "yes\rno"})
        try:
            assert container_env.client_env == {"LMER_ANSWER": "yes\rno"}
        finally:
            container_env.cleanup()

    def test_subprocess_env_overlays_os_environ(self):
        """The client keeps its own environment plus the inherited values, so
        the PATH that resolves the runtime binary is untouched."""
        container_env = build_container_env({"LMER_ANSWER": "a\nb"})
        try:
            client = container_env.subprocess_env()
            assert client["LMER_ANSWER"] == "a\nb"
            assert client.get("PATH") == os.environ.get("PATH")
        finally:
            container_env.cleanup()

    def test_subprocess_env_is_none_when_nothing_is_inherited(self):
        """The common path must be byte-identical to passing no env= at all."""
        container_env = build_container_env({"FOO": "bar"})
        try:
            assert container_env.subprocess_env() is None
        finally:
            container_env.cleanup()


class TestReservedClientNames:
    """A name the runtime client reads itself never rides its environment."""

    def test_reserved_name_with_newline_aborts_rather_than_inlining(self):
        """Corrupting the client's own HOME is not an option, and after the
        iteration-1 review neither is argv — so it aborts.

        Reachable only via a newline inside HOME/PATH/a proxy URL, which means
        a broken host and never a credential; the point is that no shape at
        all falls through to an inline value.
        """
        with pytest.raises(ContainerEnvError):
            build_container_env({"HOME": "/home/developer\nbogus"})

    def test_glob_suffixed_name_with_newline_aborts(self):
        """podman glob-expands a TRAILING ``*`` in a bare marker name, so such
        a name cannot take the marker leg — and there is nowhere else to go."""
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"WEIRD*": "a\nb"})
        assert "*" in str(excinfo.value)

    def test_interior_star_is_not_a_glob_and_rides_the_marker_leg(self):
        """Only a trailing ``*`` is special to podman's ``parseEnv``; an
        interior one is an ordinary character, so this must not over-block."""
        container_env = build_container_env({"WEIRD*NAME": "a\nb"})
        try:
            assert container_env.args == ["-e", "WEIRD*NAME"]
            assert container_env.client_env == {"WEIRD*NAME": "a\nb"}
        finally:
            container_env.cleanup()

    def test_reserved_name_with_normal_value_still_uses_the_env_file(self):
        """The reserved set only gates the marker leg — a representable HOME is
        an ordinary env-file line."""
        container_env = build_container_env({"HOME": "/home/developer"})
        try:
            assert _parsed_env_file(container_env) == {"HOME": "/home/developer"}
            assert container_env.client_env == {}
        finally:
            container_env.cleanup()


class TestFilePermissionsAndLifetime:
    """The env file is owner-only and does not outlive the session."""

    def test_file_is_0600_inside_a_0700_directory(self):
        container_env = build_container_env({"SECRET": "glpat-fixture"})
        try:
            path = container_env.env_file_dir / _ENV_FILE_NAME
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(container_env.env_file_dir.stat().st_mode) == 0o700
        finally:
            container_env.cleanup()

    def test_cleanup_removes_the_directory(self):
        container_env = build_container_env({"SECRET": "glpat-fixture"})
        directory = container_env.env_file_dir
        container_env.cleanup()
        assert not directory.exists()
        assert container_env.env_file_dir is None

    def test_cleanup_is_idempotent(self):
        """The CLI calls it from a finally that may run after an earlier
        cleanup on some paths."""
        container_env = build_container_env({"SECRET": "glpat-fixture"})
        container_env.cleanup()
        container_env.cleanup()

    def test_cleanup_without_a_file_is_a_noop(self):
        ContainerEnv().cleanup()

    def test_empty_env_writes_nothing(self):
        container_env = build_container_env({})
        assert container_env.args == []
        assert container_env.env_file_dir is None

    def test_all_none_values_write_nothing(self):
        """None values were always skipped; skipping them all means no file."""
        container_env = build_container_env({"A": None, "B": None})
        assert container_env.args == []
        assert container_env.env_file_dir is None


# ---------------------------------------------------------------------------
# End-to-end: the assembled run command for a real main() spawn.
# ---------------------------------------------------------------------------


def _spawn_and_capture(
    argv, env_in=None, home=None, real_user_mounts=False, call_side_effect=None
):
    """Run main() with the real transport and capture the launch.

    Returns ``(rc, captured)`` where ``captured`` holds the run command, the
    env handed to ``subprocess.call``, the container env dict, and a snapshot
    of the env file taken while it still existed (the CLI deletes it before
    main() returns).

    ``real_user_mounts`` — leave ``build_user_mounts`` unmocked so the
    SSH-agent branch in ``mounts.py`` can contribute its inline
    ``-e SSH_AUTH_SOCK=/ssh-agent``. Off by default: every other test here
    wants a clean command, and that pair is the one documented exception.

    ``call_side_effect`` — replaces the capturing ``subprocess.call`` stub, for
    tests that need to act while the runtime client is notionally running (the
    SIGTERM case).
    """
    env = {**_BASE_ENV, **(env_in or {})}
    if home is not None:
        env["HOME"] = str(home)
    captured: dict = {"cmd": [], "call_env": None, "container_env": {}, "file": None}

    def _wrap_build(env_dict):
        captured["container_env"].update(env_dict)
        return build_container_env(env_dict)

    def _capture_call(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["call_env"] = kwargs.get("env")
        if "--env-file" in cmd:
            path = Path(cmd[cmd.index("--env-file") + 1])
            captured["file"] = path.read_text(encoding="utf-8")
            captured["file_mode"] = stat.S_IMODE(path.stat().st_mode)
            captured["dir_mode"] = stat.S_IMODE(path.parent.stat().st_mode)
            captured["dir"] = path.parent
        return 0

    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks() as stack:
            if home is not None:
                stack.enter_context(
                    patch("lmer_cli.cli.lmer_state_dir", return_value=home / ".lmer")
                )
            stack.enter_context(
                patch(
                    "lmer_cli.cli.build_user_harness_mounts",
                    return_value=([], False),
                )
            )
            if real_user_mounts:
                # Let mounts.py's own SSH-agent branch run, so the inline pair
                # under test comes from production code rather than a fixture.
                stack.enter_context(
                    patch("lmer_cli.cli.build_user_mounts", new=_real_build_user_mounts)
                )
            # Override the harness's stubs with the real transport and a
            # launch capture.
            stack.enter_context(
                patch("lmer_cli.cli.build_container_env", side_effect=_wrap_build)
            )
            stack.enter_context(
                patch(
                    "lmer_cli.cli.subprocess.call",
                    side_effect=call_side_effect or _capture_call,
                )
            )
            from lmer_cli.cli import main

            rc = main(argv)
    return rc, captured


class TestSpawnCommandCarriesNoValues:
    """Acceptance criterion 1, asserted on the real assembled command."""

    def test_no_container_env_value_appears_in_the_run_command(self, tmp_path):
        """Not the credentialed work-repo URL, not the FastAPI bearer token,
        not a plain passthrough value."""
        rc, captured = _spawn_and_capture(
            [
                "--fastapi-token",
                _SENTINEL_FASTAPI,
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={
                "LMER_WORK_REPO": _SENTINEL_WORK_REPO,
                "LMER_REASONING_EFFORT": _SENTINEL_EFFORT,
            },
            home=tmp_path / "home",
        )
        assert rc == 0, f"spawn failed: rc={rc}"
        joined = " ".join(captured["cmd"])
        for sentinel in (_SENTINEL_WORK_REPO, _SENTINEL_FASTAPI, _SENTINEL_EFFORT):
            assert sentinel not in joined, (
                f"{sentinel!r} reached the run command — /proc/<pid>/cmdline is "
                f"world-readable. Command: {joined!r}"
            )

    def test_the_values_did_reach_the_container(self, tmp_path):
        """The negative assertion above would also pass if the transport simply
        dropped everything — so pin that the values are in the env file."""
        rc, captured = _spawn_and_capture(
            [
                "--fastapi-token",
                _SENTINEL_FASTAPI,
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={"LMER_WORK_REPO": _SENTINEL_WORK_REPO},
            home=tmp_path / "home",
        )
        assert rc == 0
        assert captured["file"] is not None, "no --env-file was passed"
        assert f"LMER_WORK_REPO={_SENTINEL_WORK_REPO}\n" in captured["file"]
        assert f"LMER_FASTAPI_TOKEN={_SENTINEL_FASTAPI}\n" in captured["file"]

    def test_every_transport_emitted_e_flag_carries_a_bare_name(self, tmp_path):
        """The structural invariant, scoped to what this MR controls.

        Deliberately NOT "no inline ``-e`` anywhere in the command": review of
        iteration 1 showed this guard cannot fail for the one path that still
        emits one, because ``_spawn_and_capture`` runs with ``_BASE_ENV``
        (no ``SSH_AUTH_SOCK``) and pins ``build_user_harness_mounts``. So the
        claim here is the honest one — every ``-e`` that
        ``build_container_env`` emits is a bare name — and the known exception
        is asserted explicitly in
        ``TestKnownInlineExceptionOutsideTheTransport``.
        """
        rc, captured = _spawn_and_capture(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_WORK_REPO": _SENTINEL_WORK_REPO},
            home=tmp_path / "home",
        )
        assert rc == 0
        cmd = captured["cmd"]
        for index, arg in enumerate(cmd):
            if arg == "-e":
                following = cmd[index + 1]
                assert "=" not in following, (
                    f"-e {following!r} carries an inline value; the transport "
                    f"must pass a bare name"
                )

    def test_env_file_is_private_and_removed_after_the_session(self, tmp_path):
        """Modes hold on the real spawn path, and nothing outlives it."""
        rc, captured = _spawn_and_capture(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_WORK_REPO": _SENTINEL_WORK_REPO},
            home=tmp_path / "home",
        )
        assert rc == 0
        assert captured["file_mode"] == 0o600
        assert captured["dir_mode"] == 0o700
        assert not captured["dir"].exists(), (
            "the env file outlived the spawn; it must be deleted once the run "
            "command returns"
        )

    def test_multiline_prompt_still_reaches_the_container(self, tmp_path):
        """The regression an env-file-only transport would have introduced.

        ``lmer --prompt "$(cat file)"`` works today because argv carries
        newlines. It must keep working — via the inheritance leg.
        """
        prompt = "do the thing\n\nthen the other thing"
        rc, captured = _spawn_and_capture(
            ["--prompt", prompt, "--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_WORK_REPO": _SENTINEL_WORK_REPO},
            home=tmp_path / "home",
        )
        assert rc == 0
        cmd = captured["cmd"]
        assert "LMER_START_PROMPT" in cmd, (
            f"expected a bare -e LMER_START_PROMPT marker; got {cmd!r}"
        )
        assert captured["call_env"] is not None, (
            "the runtime client must be started with the inherited value"
        )
        assert captured["call_env"]["LMER_START_PROMPT"] == prompt
        assert prompt not in " ".join(cmd)


class TestKnownInlineExceptionOutsideTheTransport:
    """The one inline ``-e NAME=value`` a real spawn can contain.

    ``mounts.py`` appends ``-e SSH_AUTH_SOCK=/ssh-agent`` whenever an agent
    socket is present, so on an agent-forwarding host the run command does
    hold an inline pair. It carries a fixed container-side socket path, not a
    forwarded value, and is deliberately left alone — but a guard whose
    silence reads as "no inline ``-e`` anywhere" would be misleading, so the
    exception is pinned here rather than left to the reader.
    """

    def test_ssh_agent_forwarding_adds_exactly_one_known_inline_pair(self, tmp_path):
        """With SSH forwarding on, the ONLY inline pair is SSH_AUTH_SOCK."""
        sock = tmp_path / "agent.sock"
        sock.write_text("")
        rc, captured = _spawn_and_capture(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={
                "LMER_WORK_REPO": _SENTINEL_WORK_REPO,
                "SSH_AUTH_SOCK": str(sock),
            },
            home=tmp_path / "home",
            real_user_mounts=True,
        )
        assert rc == 0
        cmd = captured["cmd"]
        inline = [
            cmd[index + 1]
            for index, arg in enumerate(cmd)
            if arg == "-e" and "=" in cmd[index + 1]
        ]
        assert inline == ["SSH_AUTH_SOCK=/ssh-agent"], (
            f"expected exactly the known SSH_AUTH_SOCK exception; got {inline!r}"
        )
        # It carries the fixed container-side path: the HOST socket path
        # reaches the command only as the -v mount source, never as a value.
        e_operands = [
            cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-e"
        ]
        assert not any(str(sock) in operand for operand in e_operands)
        assert _SENTINEL_WORK_REPO not in " ".join(cmd)


class TestSigtermRunsCleanup:
    """SIGTERM must delete the env file WITHOUT killing the runtime client.

    Two failure modes, and the fix has to avoid both:

    - Python's default SIGTERM disposition exits without unwinding, so the
      launch path's ``finally`` never ran and the mode-0600 file — holding the
      tokenized work-repo URL and every forwarded token — stayed in
      ``$TMPDIR`` for good.
    - Raising ``SystemExit`` from the handler to fix that unwinds through
      ``subprocess.call``'s bare ``except: p.kill()``, SIGKILLing the
      ``docker``/``podman run`` client. In a non-TTY spawn the client
      forwarding SIGTERM is what stops the container (``--sig-proxy`` is
      non-TTY-only) and ``--rm`` removal is daemon-side and conditional on
      that exit, so a reaped session would leave a running container behind.

    So this runs OUT OF PROCESS against the real ``subprocess.call``: an
    in-process test cannot see the second failure mode (it substitutes
    ``subprocess.call``), and cannot host the handler either, since the handler
    deliberately re-raises the signal at its own process.
    """

    def _write_probe_scripts(self, tmp_path):
        child = tmp_path / "child.py"
        child.write_text(
            "import os, pathlib, signal, sys, time\n"
            "log = pathlib.Path(sys.argv[1])\n"
            "def onterm(signum, frame):\n"
            "    log.write_text(log.read_text() + 'GOT\\n')\n"
            "    time.sleep(0.3)\n"
            "    log.write_text(log.read_text() + 'DONE\\n')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, onterm)\n"
            # The pid rides the READY line so the parent-alone test can assert
            # the client is ALIVE rather than merely unlogged — see there.
            "log.write_text('READY %d\\n' % os.getpid())\n"
            "time.sleep(30)\n"
        )
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import pathlib, signal, subprocess, sys, tempfile\n"
            f"sys.path.insert(0, {str(_SRC_DIR)!r})\n"
            "from lmer_cli.cli import _make_sigterm_cleanup_handler\n"
            "from lmer_cli.runtime import ContainerEnv, _ENV_FILE_NAME\n"
            "envdir = pathlib.Path(tempfile.mkdtemp(prefix='lmer-env-probe-'))\n"
            "(envdir / _ENV_FILE_NAME).write_text('TOKEN=glpat-probe\\n')\n"
            "container_env = ContainerEnv(env_file_dir=envdir)\n"
            "print(envdir, flush=True)\n"
            "signal.signal(signal.SIGTERM, "
            "_make_sigterm_cleanup_handler(container_env))\n"
            "subprocess.call([sys.executable, sys.argv[1], sys.argv[2]])\n"
        )
        return parent, child

    def _await(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _child_pid(log):
        """The probe child's pid, off the READY line."""
        match = re.match(r"READY (\d+)", log.read_text())
        assert match, f"no pid on the READY line: {log.read_text()!r}"
        return int(match.group(1))

    @staticmethod
    def _is_running(pid):
        """Whether ``pid`` exists and is not a zombie.

        ``/proc/<pid>`` alone is not enough: a SIGKILLed orphan lingers as a
        zombie until its reaper gets to it, so the directory can outlive the
        very kill this asserts against.
        """
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        # State is the field after the parenthesised comm, which may itself
        # contain spaces — rsplit past it.
        return stat_line.rsplit(") ", 1)[1].split(" ", 1)[0] != "Z"

    def test_reaped_session_cleans_up_and_the_client_still_shuts_down(
        self, tmp_path
    ):
        """The reaper case: SIGTERM to the process group.

        The child stands in for the runtime client — it handles SIGTERM, takes
        300ms (forwarding the stop and waiting for the container), then records
        completion. If the handler SIGKILLs it, 'DONE' never appears.
        """
        parent_script, child_script = self._write_probe_scripts(tmp_path)
        log = tmp_path / "child.log"
        proc = subprocess.Popen(
            [sys.executable, str(parent_script), str(child_script), str(log)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            env_dir = Path(proc.stdout.readline().strip())
            assert env_dir.is_dir(), "probe did not create its env dir"
            assert self._await(lambda: log.exists() and "READY" in log.read_text()), (
                "child never started"
            )

            os.killpg(proc.pid, signal.SIGTERM)

            assert self._await(lambda: "DONE" in log.read_text()), (
                f"the runtime client never completed its own SIGTERM handling — "
                f"it was killed. log={log.read_text()!r}"
            )
            assert self._await(lambda: not env_dir.exists()), (
                f"{env_dir} survived SIGTERM; the credential-bearing env file "
                f"must not outlive the spawn"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def test_sigterm_to_the_parent_alone_leaves_the_client_running(self, tmp_path):
        """Signalling lmer alone must not take the container down with it.

        This is the behaviour ``02f6d6a`` had and the SystemExit version broke:
        the client was SIGKILLed before it ever saw a signal of its own.

        The load-bearing assertion is that the child is still RUNNING. "'GOT'
        never appeared" alone is satisfied by both the correct outcome (never
        signalled) and the regression (SIGKILLed before it could log), so it
        pins nothing on its own.
        """
        parent_script, child_script = self._write_probe_scripts(tmp_path)
        log = tmp_path / "child.log"
        proc = subprocess.Popen(
            [sys.executable, str(parent_script), str(child_script), str(log)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        child_pid = None
        try:
            env_dir = Path(proc.stdout.readline().strip())
            assert self._await(lambda: log.exists() and "READY" in log.read_text())
            child_pid = self._child_pid(log)

            proc.send_signal(signal.SIGTERM)

            assert self._await(lambda: not env_dir.exists()), "env dir not cleaned"
            assert self._await(lambda: proc.poll() is not None), "lmer did not exit"
            # Outlived the parent, unsignalled: exactly as before the handler
            # existed. SIGKILL from subprocess.call's bare `except` would have
            # left this pid gone or a zombie.
            assert self._is_running(child_pid), (
                f"the runtime client (pid {child_pid}) did not survive a SIGTERM "
                f"aimed at lmer alone. log={log.read_text()!r}"
            )
            assert "GOT" not in log.read_text(), (
                "the client was signalled when only lmer was targeted"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            # The child is orphaned by design here, so nothing else will reap
            # it — without this it sleeps out its full 30s per test run.
            if child_pid is not None:
                with suppress(OSError):
                    os.kill(child_pid, signal.SIGKILL)

    def test_sigterm_disposition_is_restored_after_a_normal_run(self, tmp_path):
        previous = signal.getsignal(signal.SIGTERM)
        rc, _ = _spawn_and_capture(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_WORK_REPO": _SENTINEL_WORK_REPO},
            home=tmp_path / "home",
        )
        assert rc == 0
        assert signal.getsignal(signal.SIGTERM) is previous


class TestInputsNoLegCanCarry:
    """Refused up front, because neither transport can deliver them.

    Both are properties of the name/value pair alone, checked before either leg
    is tried and before any file is written. Iteration 2 established by probe
    that the previous approach here was wrong: `errors="surrogateescape"` wrote
    the raw byte, and docker's env-file parser runs `utf8.Valid` per line and
    aborted the entire spawn (`invalid utf8 bytes at line N`, rc 125) — taking
    every other variable with it. The marker leg is no better: the client
    marshals `Config.Env` as JSON, so the byte arrives as U+FFFD, which is also
    what the pre-#158 argv form silently did. "Original bytes" was never on the
    menu, so lmer refuses loudly instead of corrupting quietly.
    """

    def test_non_utf8_value_is_refused_naming_the_variable(self):
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"OK": "plain", "WEIRD": "\udcff"})
        message = str(excinfo.value)
        assert "WEIRD" in message
        assert "UTF-8" in message
        assert "U+FFFD" in message, (
            "the message should say what would otherwise happen silently"
        )

    def test_non_utf8_name_is_refused_rather_than_raising(self):
        """A surrogate in a NAME used to escape as a ``UnicodeEncodeError``.

        The env-file blocker measures the encoded ``NAME=value`` line, so it
        raised from inside the blocker — past ``cli.py``'s ``ContainerEnvError``
        handler and out as a traceback. Latent rather than live (every input
        path decodes as UTF-8 before a key reaches lmer), but the refusal
        wording already existed; the name rides the same JSON marshalling and
        the same per-line ``utf8.Valid`` as a value, so no leg can deliver it
        either.
        """
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"FO\udcffO": "plainvalue"})
        message = str(excinfo.value)
        assert "FO\udcffO" in message
        assert "name is not valid UTF-8" in message

    def test_nul_in_a_name_is_refused(self):
        """The marker leg refused it; the env-file leg wrote the raw byte.

        Same split verdict iteration 2 closed for values, with the legs
        swapped — ``build_container_env({'FO\\0O': 'x'})`` produced the line
        ``b'FO\\x00O=x\\n'`` in the file. ``_NUL``'s "no transport can carry"
        comment is now true of both positions.
        """
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"FO\0O": "x"})
        assert "name contains a NUL byte" in str(excinfo.value)

    def test_nul_in_a_value_is_refused(self):
        """Previously unblocked on both legs: the env file carried it to docker
        and `execve` truncated the value inside the container, while the marker
        leg raised an uncaught `ValueError: embedded null byte` out of
        `subprocess`. Neither is a delivery.
        """
        with pytest.raises(ContainerEnvError) as excinfo:
            build_container_env({"FOO": "a\0b"})
        assert "NUL" in str(excinfo.value)

    def test_refusal_happens_before_any_file_is_written(self, monkeypatch):
        """A refusal must not strand a directory."""
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
        with pytest.raises(ContainerEnvError):
            build_container_env({"GOOD": "value", "WEIRD": "\udcff"})
        assert created == [], "no env file should be written before the refusal"
