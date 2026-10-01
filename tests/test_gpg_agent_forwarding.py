"""Mechanics of gpg-agent forwarding for ansible-taskdef sessions (#355).

Three layers, all exercising the shipped code rather than a re-implementation:

1. ``mounts.py`` resolvers and mount builder — how the host extra socket and
   public keyring are found, guarded, and turned into ``-v`` arguments.
2. The ``entrypoint.sh`` GPG block — extracted from the real script and
   executed (the ``tests/test_git_user_override.py`` precedent), so its
   inert-by-default and never-fatal promises are tested where they live.
3. A live redirect probe: a fake Assuan agent on a unix socket, reached
   through the provisioned GNUPGHOME by the real ``gpg-connect-agent``. This
   is what turns "modern gpg reads the ``%Assuan%`` redirect file" from a
   documented expectation into a measured fact on the image under test.

The scoping half — who is allowed to receive any of this — lives in
``tests/test_ansible_gpg_scoping.py``.
"""

import os
import re
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.mounts import (
    CONTAINER_GPG_AGENT_SOCKET_PATH,
    CONTAINER_GPG_PUBRING_PREFIX,
    build_gpg_agent_mounts,
    container_gpg_pubring_path,
    resolve_gpg_extra_socket,
    resolve_gpg_pubring,
)

REPO_ROOT = Path(__file__).parent.parent
CLI_PY = REPO_ROOT / "src" / "lmer_cli" / "cli.py"
ENTRYPOINT = REPO_ROOT / "Ctl" / "container" / "entrypoint.sh"

# Markers delimiting the gpg block inside entrypoint.sh. Kept in sync with the
# script's own comments; renaming the block fails these tests loudly rather
# than silently passing on stale code.
BLOCK_START = "# ── GPG agent forwarding ──"
BLOCK_END = "# Check if we're in a git repository"


@pytest.fixture
def agent_socket(tmp_path):
    """A real listening unix socket standing in for the host agent's extra socket."""
    path = tmp_path / "S.gpg-agent.extra"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    yield path
    sock.close()


@pytest.fixture
def gpg_home(tmp_path):
    """A host gpg home holding a (dummy) keybox-format public keyring."""
    home = tmp_path / "gnupg"
    home.mkdir()
    # 'KBXf' at offset 8 is the keybox magic; the content is never parsed by
    # lmer, only mounted.
    (home / "pubring.kbx").write_bytes(b"\x00" * 8 + b"KBXf")
    return home


class TestResolveExtraSocket:
    def test_explicit_path_to_a_socket_is_accepted(self, agent_socket):
        resolved, reason = resolve_gpg_extra_socket(str(agent_socket))
        assert reason is None
        assert resolved == agent_socket

    def test_surrounding_whitespace_is_tolerated(self, agent_socket):
        resolved, reason = resolve_gpg_extra_socket(f"  {agent_socket}  ")
        assert reason is None
        assert resolved == agent_socket

    def test_missing_path_is_refused_by_name(self, tmp_path):
        resolved, reason = resolve_gpg_extra_socket(str(tmp_path / "absent"))
        assert resolved is None
        assert "does not exist" in reason
        assert "absent" in reason

    def test_regular_file_is_refused(self, tmp_path):
        plain = tmp_path / "not-a-socket"
        plain.write_text("")
        resolved, reason = resolve_gpg_extra_socket(str(plain))
        assert resolved is None
        assert "is not a socket" in reason

    def test_a_socket_outside_the_home_is_accepted(self, agent_socket):
        """The extra socket lives under /run/user/<uid>/gnupg on a normal host,
        so the release key's under-home rule must NOT apply here."""
        assert not str(agent_socket).startswith(str(Path.home()))
        resolved, reason = resolve_gpg_extra_socket(str(agent_socket))
        assert reason is None and resolved == agent_socket

    def test_auto_asks_gpgconf(self, agent_socket):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{agent_socket}\n", stderr=""
        )
        with patch("lmer_cli.mounts.subprocess.run", return_value=completed) as run:
            resolved, reason = resolve_gpg_extra_socket("auto")
        assert reason is None
        assert resolved == agent_socket
        assert run.call_args[0][0] == [
            "gpgconf",
            "--list-dirs",
            "agent-extra-socket",
        ], "auto must read gpgconf's own output for the extra socket"

    def test_auto_is_case_insensitive(self, agent_socket):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{agent_socket}\n", stderr=""
        )
        with patch("lmer_cli.mounts.subprocess.run", return_value=completed):
            resolved, reason = resolve_gpg_extra_socket("AUTO")
        assert reason is None and resolved == agent_socket

    def test_auto_reports_a_failing_gpgconf(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="gpgconf: no such option\n"
        )
        with patch("lmer_cli.mounts.subprocess.run", return_value=completed):
            resolved, reason = resolve_gpg_extra_socket("auto")
        assert resolved is None
        assert "gpgconf" in reason and "no such option" in reason

    def test_auto_reports_a_missing_gpgconf(self):
        with patch(
            "lmer_cli.mounts.subprocess.run", side_effect=FileNotFoundError("gpgconf")
        ):
            resolved, reason = resolve_gpg_extra_socket("auto")
        assert resolved is None
        assert "cannot run" in reason and "gpgconf" in reason

    def test_auto_reports_empty_gpgconf_output(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n", stderr=""
        )
        with patch("lmer_cli.mounts.subprocess.run", return_value=completed):
            resolved, reason = resolve_gpg_extra_socket("auto")
        assert resolved is None
        assert "printed no path" in reason


class TestResolvePubring:
    def test_derives_kbx_from_gnupghome(self, gpg_home):
        resolved, reason = resolve_gpg_pubring(None, env={"GNUPGHOME": str(gpg_home)})
        assert reason is None
        assert resolved == gpg_home / "pubring.kbx"

    def test_falls_back_to_the_legacy_gpg_keyring(self, gpg_home):
        (gpg_home / "pubring.kbx").unlink()
        (gpg_home / "pubring.gpg").write_bytes(b"\x99legacy")
        resolved, reason = resolve_gpg_pubring(None, env={"GNUPGHOME": str(gpg_home)})
        assert reason is None
        assert resolved == gpg_home / "pubring.gpg"

    def test_kbx_wins_when_both_exist(self, gpg_home):
        (gpg_home / "pubring.gpg").write_bytes(b"\x99legacy")
        resolved, _ = resolve_gpg_pubring(None, env={"GNUPGHOME": str(gpg_home)})
        assert resolved == gpg_home / "pubring.kbx"

    def test_no_candidate_refuses_and_points_at_the_override(self, tmp_path):
        resolved, reason = resolve_gpg_pubring(
            None, env={"GNUPGHOME": str(tmp_path / "empty")}
        )
        assert resolved is None
        assert "pubring.kbx" in reason and "pubring.gpg" in reason
        assert "LMER_ANSIBLE_GPG_PUBRING" in reason, (
            "The refusal must name the escape hatch — this is the whole reason "
            "the override variable exists"
        )

    def test_no_guessing_beyond_the_two_candidates(self, gpg_home):
        """A keyring under some other name is NOT found by derivation."""
        (gpg_home / "pubring.kbx").rename(gpg_home / "vault-recipients.kbx")
        resolved, reason = resolve_gpg_pubring(None, env={"GNUPGHOME": str(gpg_home)})
        assert resolved is None and reason is not None

    def test_explicit_override_names_any_keyring(self, gpg_home):
        named = gpg_home / "vault-recipients.kbx"
        (gpg_home / "pubring.kbx").rename(named)
        resolved, reason = resolve_gpg_pubring(str(named))
        assert reason is None and resolved == named

    def test_explicit_override_may_live_outside_the_home(self, tmp_path):
        """The operator's keyring may legitimately sit outside $HOME — the
        directory they named is its own containment root."""
        outside = tmp_path / "shared" / "vault.kbx"
        outside.parent.mkdir()
        outside.write_bytes(b"\x00" * 8 + b"KBXf")
        assert not str(outside).startswith(str(Path.home()))
        resolved, reason = resolve_gpg_pubring(str(outside))
        assert reason is None and resolved == outside

    def test_missing_explicit_override_is_refused(self, tmp_path):
        resolved, reason = resolve_gpg_pubring(str(tmp_path / "absent.kbx"))
        assert resolved is None
        assert "does not exist" in reason

    def test_directory_is_refused(self, gpg_home, tmp_path):
        as_dir = tmp_path / "keyring.kbx"
        as_dir.mkdir()
        resolved, reason = resolve_gpg_pubring(str(as_dir))
        assert resolved is None
        assert "not a regular file" in reason

    def test_unknown_suffix_is_refused_rather_than_guessed(self, gpg_home):
        odd = gpg_home / "recipients.dat"
        odd.write_bytes(b"whatever")
        resolved, reason = resolve_gpg_pubring(str(odd))
        assert resolved is None
        assert "*.kbx" in reason and "*.gpg" in reason, (
            "gpg picks its keyring parser from the file name, so an unknown "
            "suffix must be refused, not mounted under a guessed name"
        )

    def test_symlink_escaping_the_named_directory_is_refused(self, tmp_path):
        """The containment rule: a symlink must not swing the mount onto a
        keyring naming recipients the operator never chose."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real = elsewhere / "other-recipients.kbx"
        real.write_bytes(b"\x00" * 8 + b"KBXf")
        named_dir = tmp_path / "named"
        named_dir.mkdir()
        link = named_dir / "vault.kbx"
        link.symlink_to(real)
        with patch("lmer_cli.mounts.Path.home", return_value=named_dir):
            resolved, reason = resolve_gpg_pubring(str(link))
        assert resolved is None
        assert "outside both" in reason
        assert str(elsewhere) in reason

    def test_symlink_inside_the_named_directory_is_accepted(self, tmp_path):
        named_dir = tmp_path / "named"
        named_dir.mkdir()
        real = named_dir / "real.kbx"
        real.write_bytes(b"\x00" * 8 + b"KBXf")
        link = named_dir / "vault.kbx"
        link.symlink_to(real)
        with patch("lmer_cli.mounts.Path.home", return_value=tmp_path / "nowhere"):
            resolved, reason = resolve_gpg_pubring(str(link))
        assert reason is None and resolved == link


class TestBuildMounts:
    def test_both_binds_are_read_only(self, agent_socket, gpg_home):
        pubring = gpg_home / "pubring.kbx"
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            args = build_gpg_agent_mounts("podman", agent_socket, pubring)
        assert args == [
            "-v",
            f"{agent_socket}:{CONTAINER_GPG_AGENT_SOCKET_PATH}:ro",
            "-v",
            f"{pubring}:{CONTAINER_GPG_PUBRING_PREFIX}.kbx:ro",
        ]

    def test_selinux_label_suffix_is_applied(self, agent_socket, gpg_home):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            args = build_gpg_agent_mounts("podman", agent_socket, gpg_home / "pubring.kbx")
        assert all(spec.endswith(":ro,z") for spec in args[1::2])

    def test_container_keyring_path_carries_the_host_suffix(self):
        assert container_gpg_pubring_path(Path("/h/pubring.kbx")) == "/gpg-pubring.kbx"
        assert container_gpg_pubring_path(Path("/h/pubring.gpg")) == "/gpg-pubring.gpg"


def _extract_block() -> str:
    """Return the gpg block from the real entrypoint.sh."""
    text = ENTRYPOINT.read_text()
    assert BLOCK_START in text, f"entrypoint marker missing: {BLOCK_START!r}"
    assert BLOCK_END in text, f"entrypoint marker missing: {BLOCK_END!r}"
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END, start)
    return text[start:end]


def _run_block(home: Path, sock=None, pubring=None, path=None):
    """Execute the extracted entrypoint block under ``set -e``.

    ``set -e`` mirrors the real entrypoint, which runs under it — that is what
    makes "never fails a launch" testable rather than asserted in a comment.

    ``path`` replaces PATH, so a test can put a fake ``gpgconf`` in front of
    the real one (or take gpgconf away entirely).
    """
    script = "set -e\n" + _extract_block() + '\necho "GNUPGHOME=${GNUPGHOME:-}"\n'
    env = {"PATH": path or "/usr/bin:/bin", "HOME": str(home)}
    if sock is not None:
        env["LMER_ANSIBLE_GPG_AGENT"] = str(sock)
    if pubring is not None:
        env["LMER_ANSIBLE_GPG_PUBRING"] = str(pubring)
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    return proc


class TestEntrypointBlock:
    """Inert unless the socket mount is present; never fatal."""

    def test_no_variable_provisions_nothing(self, tmp_path):
        proc = _run_block(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / ".lmer-gnupg").exists()
        assert "GNUPGHOME=\n" in proc.stdout
        assert "gpg" not in proc.stdout.lower().replace("gnupghome=", "")

    def test_variable_set_but_no_socket_mounted_provisions_nothing(self, tmp_path):
        """A stale variable with no mount behind it must not build a GNUPGHOME
        that points gpg at a socket that is not there."""
        proc = _run_block(tmp_path, sock=tmp_path / "never-mounted")
        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / ".lmer-gnupg").exists()

    def test_a_regular_file_is_not_mistaken_for_the_mount(self, tmp_path):
        plain = tmp_path / "plain"
        plain.write_text("")
        proc = _run_block(tmp_path, sock=plain)
        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / ".lmer-gnupg").exists()

    def test_socket_present_provisions_the_redirect(self, tmp_path, agent_socket):
        path = TestRedirectFollowsGpgconfSocketdir._fake_gpgconf(
            tmp_path / "bin", tmp_path / ".lmer-gnupg"
        )
        proc = _run_block(tmp_path, sock=agent_socket, path=path)
        assert proc.returncode == 0, proc.stderr
        gnupg = tmp_path / ".lmer-gnupg"
        assert gnupg.is_dir()
        assert stat.S_IMODE(gnupg.stat().st_mode) == 0o700
        assert (gnupg / "S.gpg-agent").read_text() == f"%Assuan%\nsocket={agent_socket}\n"
        assert f"GNUPGHOME={gnupg}" in proc.stdout

    def test_the_home_is_deliberately_not_the_default_gnupg_dir(self, tmp_path, agent_socket):
        """The separate home keeps the session clear of any real ~/.gnupg.

        It buys nothing against the socket directory — gnupg picks that from
        /run/user/<uid> when that exists, whatever the home is called — which
        is why the redirect is written where gpgconf points rather than in the
        home (see TestRedirectFollowsGpgconfSocketdir)."""
        _run_block(tmp_path, sock=agent_socket)
        assert not (tmp_path / ".gnupg").exists()
        assert (tmp_path / ".lmer-gnupg").is_dir()

    def test_keyring_is_copied_under_the_name_gpg_looks_for(self, tmp_path, agent_socket):
        mounted = tmp_path / "gpg-pubring.kbx"
        mounted.write_bytes(b"\x00" * 8 + b"KBXf")
        proc = _run_block(tmp_path, sock=agent_socket, pubring=mounted)
        assert proc.returncode == 0, proc.stderr
        installed = tmp_path / ".lmer-gnupg" / "pubring.kbx"
        assert installed.read_bytes() == mounted.read_bytes(), (
            "gpg reads 'pubring.kbx', never the mount's own basename"
        )

    def test_legacy_keyring_suffix_is_preserved(self, tmp_path, agent_socket):
        mounted = tmp_path / "gpg-pubring.gpg"
        mounted.write_bytes(b"\x99legacy")
        _run_block(tmp_path, sock=agent_socket, pubring=mounted)
        assert (tmp_path / ".lmer-gnupg" / "pubring.gpg").exists()
        assert not (tmp_path / ".lmer-gnupg" / "pubring.kbx").exists()

    def test_missing_keyring_warns_without_failing(self, tmp_path, agent_socket):
        path = TestRedirectFollowsGpgconfSocketdir._fake_gpgconf(
            tmp_path / "bin", tmp_path / ".lmer-gnupg"
        )
        proc = _run_block(tmp_path, sock=agent_socket, pubring=tmp_path / "absent.kbx", path=path)
        assert proc.returncode == 0, proc.stderr
        assert "keyring" in proc.stdout
        assert (tmp_path / ".lmer-gnupg" / "S.gpg-agent").exists(), (
            "A missing keyring must not cost the session the agent redirect"
        )

    def test_unprovisionable_home_warns_without_failing(self, tmp_path, agent_socket):
        """The never-fatal promise, at the one step that can realistically fail.

        The home is made unusable by being a regular *file*, so the `mkdir`
        fails with ENOTDIR — a failure no uid can walk past. An earlier
        version of this test used a `0o500` directory, which is only
        unwritable for a non-root user: it passed locally and failed in CI,
        where the suite runs as root.
        """
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("")
        proc = _run_block(blocked, sock=agent_socket)
        assert proc.returncode == 0, (
            f"A failed provisioning step must not abort the launch: {proc.stderr}"
        )
        assert "Could not provision" in proc.stdout
        assert "gpg-agent forwarded" not in proc.stdout, (
            "Success must not be reported when provisioning failed"
        )


class TestRedirectFollowsGpgconfSocketdir:
    """Where the redirect goes is gnupg's choice, and gpgconf reports it.

    gpg opens ``S.gpg-agent`` inside its *socket directory*, which is under
    ``/run/user/<uid>/gnupg`` when that runtime dir exists and the home
    directory only as a fallback when it does not. Writing into the home
    unconditionally works only in a container that happens to lack
    ``/run/user/<uid>`` — an environment property nothing here owns. These
    tests pin the invariant instead: the redirect lands wherever gpgconf
    says gpg will look.
    """

    @staticmethod
    def _fake_gpgconf(bin_dir: Path, socketdir: Path) -> str:
        """A gpgconf on PATH reporting *socketdir*, as one under /run/user would."""
        bin_dir.mkdir(parents=True, exist_ok=True)
        fake = bin_dir / "gpgconf"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--list-dirs" ] && [ "$2" = "socketdir" ]; then\n'
            f'    echo "{socketdir}"\n'
            "    exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        fake.chmod(0o755)
        return f"{bin_dir}:/usr/bin:/bin"

    def test_redirect_is_written_into_the_reported_socketdir(self, tmp_path, agent_socket):
        socketdir = tmp_path / "run" / "gnupg" / "d.hashed"
        path = self._fake_gpgconf(tmp_path / "bin", socketdir)
        home = tmp_path / "home"
        home.mkdir()
        proc = _run_block(home, sock=agent_socket, path=path)
        assert proc.returncode == 0, proc.stderr
        assert (socketdir / "S.gpg-agent").read_text() == (
            f"%Assuan%\nsocket={agent_socket}\n"
        ), "The redirect must land where gpg will open it, not where we assumed"
        assert not (home / ".lmer-gnupg" / "S.gpg-agent").exists(), (
            "Writing only into the home is what breaks the moment a container "
            "gains /run/user/<uid>"
        )

    def test_gnupghome_still_points_at_the_provisioned_home(self, tmp_path, agent_socket):
        """The socket dir moves; the home — which holds the keyring — does not."""
        socketdir = tmp_path / "run" / "gnupg" / "d.hashed"
        path = self._fake_gpgconf(tmp_path / "bin", socketdir)
        home = tmp_path / "home"
        home.mkdir()
        pubring = tmp_path / "gpg-pubring.kbx"
        pubring.write_bytes(b"\x00" * 8 + b"KBXf")
        proc = _run_block(home, sock=agent_socket, pubring=pubring, path=path)
        assert f"GNUPGHOME={home / '.lmer-gnupg'}" in proc.stdout
        assert (home / ".lmer-gnupg" / "pubring.kbx").exists()

    def test_reported_socketdir_is_created_when_absent(self, tmp_path, agent_socket):
        socketdir = tmp_path / "run" / "gnupg" / "d.hashed"
        path = self._fake_gpgconf(tmp_path / "bin", socketdir)
        home = tmp_path / "home"
        home.mkdir()
        assert not socketdir.exists()
        proc = _run_block(home, sock=agent_socket, path=path)
        assert proc.returncode == 0, proc.stderr
        assert stat.S_IMODE(socketdir.stat().st_mode) == 0o700

    def test_without_gpgconf_it_falls_back_to_the_home(self, tmp_path, agent_socket):
        """No gpgconf on PATH: the home is where gpg looks whenever there is no
        /run/user dir, so the fallback is the same answer, just unverified."""
        # A PATH carrying everything the block needs EXCEPT gpgconf.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("bash", "mkdir", "chmod", "cp"):
            real = shutil.which(tool)
            assert real, f"{tool} not found — cannot build a gpgconf-less PATH"
            (bin_dir / tool).symlink_to(real)
        home = tmp_path / "home"
        home.mkdir()
        proc = _run_block(home, sock=agent_socket, path=str(bin_dir))
        assert proc.returncode == 0, proc.stderr
        assert (home / ".lmer-gnupg" / "S.gpg-agent").exists()

    def test_a_failing_gpgconf_does_not_abort_the_launch(self, tmp_path, agent_socket):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gpgconf"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        proc = _run_block(home, sock=agent_socket, path=f"{bin_dir}:/usr/bin:/bin")
        assert proc.returncode == 0, proc.stderr
        assert (home / ".lmer-gnupg" / "S.gpg-agent").exists()


class TestExplicitPathCannotNameTheMainSocket:
    """The extra-socket guarantee, on the branch where it can go wrong.

    ``auto`` takes gpgconf's ``agent-extra-socket`` answer and so cannot name
    the main socket. An explicit path can — and ``S.gpg-agent`` is the
    obvious-looking of the two names — while a client on the main socket is
    unrestricted: it can export secret keys, which is exactly what this
    feature promises cannot happen.
    """

    @staticmethod
    def _gpgconf_answering(main: Path, extra: Path):
        """Patch the gpgconf query as the real one answers on a normal host."""
        answers = {"agent-socket": main, "agent-extra-socket": extra}

        def _fake(key):
            return answers[key], None

        return patch("lmer_cli.mounts._gpgconf_dir", side_effect=_fake)

    def test_the_main_socket_named_by_gpgconf_is_refused(self, tmp_path):
        main = tmp_path / "S.gpg-agent"
        extra = tmp_path / "S.gpg-agent.extra"
        for path in (main, extra):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
        with self._gpgconf_answering(main, extra):
            resolved, reason = resolve_gpg_extra_socket(str(main))
        assert resolved is None, "The main socket must never be forwarded"
        assert "MAIN socket" in reason
        assert "export secret keys" in reason
        assert str(extra) in reason, "The refusal must name what to use instead"

    def test_the_extra_socket_named_by_gpgconf_is_accepted(self, tmp_path):
        main = tmp_path / "S.gpg-agent"
        extra = tmp_path / "S.gpg-agent.extra"
        for path in (main, extra):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
        with self._gpgconf_answering(main, extra):
            resolved, reason = resolve_gpg_extra_socket(str(extra))
        assert reason is None and resolved == extra

    def test_a_symlink_to_the_main_socket_is_refused(self, tmp_path):
        """Resolved paths are compared, so an alias cannot walk past the check."""
        main = tmp_path / "S.gpg-agent"
        extra = tmp_path / "S.gpg-agent.extra"
        for path in (main, extra):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
        alias = tmp_path / "agent.sock"
        alias.symlink_to(main)
        with self._gpgconf_answering(main, extra):
            resolved, reason = resolve_gpg_extra_socket(str(alias))
        assert resolved is None
        assert "MAIN socket" in reason

    def test_name_fallback_refuses_when_gpgconf_cannot_answer(self, tmp_path):
        """gpgconf speaks only for the home it resolves; a socket under another
        home falls to the naming convention — which is why the fallback demands
        the .extra sibling it would have the operator use."""
        main = tmp_path / "S.gpg-agent"
        extra = tmp_path / "S.gpg-agent.extra"
        for path in (main, extra):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
        with patch(
            "lmer_cli.mounts._gpgconf_dir", return_value=(None, "gpgconf missing")
        ):
            resolved, reason = resolve_gpg_extra_socket(str(main))
        assert resolved is None
        assert "looks like the gpg-agent MAIN socket" in reason
        assert str(extra) in reason

    def test_a_lone_socket_named_s_gpg_agent_is_accepted(self, tmp_path):
        """Trusted-as-given, and deliberately: with no .extra beside it and
        gpgconf not naming it, a socket at that path is indistinguishable from
        a deliberately-placed forwarding socket."""
        lone = tmp_path / "S.gpg-agent"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(lone))
        with patch(
            "lmer_cli.mounts._gpgconf_dir", return_value=(None, "gpgconf missing")
        ):
            resolved, reason = resolve_gpg_extra_socket(str(lone))
        assert reason is None and resolved == lone

    def test_auto_is_not_subjected_to_the_guard(self, tmp_path):
        """auto is already the producer's own answer; running the main-socket
        check against it could only ever refuse gpgconf's own extra socket."""
        extra = tmp_path / "S.gpg-agent.extra"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(extra))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{extra}\n", stderr=""
        )
        with patch("lmer_cli.mounts.subprocess.run", return_value=completed) as run:
            resolved, reason = resolve_gpg_extra_socket("auto")
        assert reason is None and resolved == extra
        assert [c[0][0][2] for c in run.call_args_list] == ["agent-extra-socket"], (
            "auto must ask for the extra socket and nothing else"
        )


@pytest.mark.skipif(
    shutil.which("gpg-connect-agent") is None,
    reason="gpg-connect-agent not installed in this environment",
)
def test_provisioned_home_makes_gpg_dial_the_forwarded_socket(tmp_path):
    """End-to-end: the real gpg client, through the real entrypoint block,
    reaches a fake agent listening on the forwarded socket path.

    Without this the redirect file is a documented expectation; with it, it is
    a measured property of the gpg build actually shipped in the image.
    """
    sock_path = tmp_path / "S.gpg-agent.extra"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    seen = {}

    def _fake_agent():
        try:
            conn, _ = server.accept()
        except OSError:  # pragma: no cover - only on teardown races
            return
        with conn:
            conn.sendall(b"OK fake agent\n")
            seen["request"] = conn.recv(1024)
            conn.sendall(b"OK\n")

    thread = threading.Thread(target=_fake_agent, daemon=True)
    thread.start()
    try:
        home = tmp_path / "home"
        home.mkdir()
        proc = _run_block(home, sock=sock_path)
        assert proc.returncode == 0, proc.stderr
        subprocess.run(
            ["gpg-connect-agent", "--no-autostart", "/bye"],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "GNUPGHOME": str(home / ".lmer-gnupg"),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        thread.join(timeout=10)
    finally:
        server.close()

    assert seen.get("request"), (
        "gpg never dialed the socket named by the %Assuan% redirect file — the "
        "forwarding mechanism does not hold on this gpg build"
    )


class TestCliEnvDictWiring:
    """Source-level guards: both variables must stay in cli.py's container env
    dict, which is built inline in main() (the LMER_GIT_USER_* precedent)."""

    def test_env_dict_spreads_the_gpg_entries(self):
        source = CLI_PY.read_text()
        assert re.search(r"\*\*gpg_env_entries,", source), (
            "gpg_env_entries missing from cli.py's container env dict — without "
            "it the None seeds vanish and the .env/preset leak paths reopen"
        )

    def test_gate_is_called_with_the_resolved_task_id(self):
        source = CLI_PY.read_text()
        assert re.search(
            r"_ansible_gpg_env\(\s*runtime,\s*task_id\s*\)", source
        ), "the gpg gate must key on the resolved task id"
