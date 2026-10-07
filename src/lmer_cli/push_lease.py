"""Explicit branch leases and source pinning for gate-push."""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
import re
import selectors
import subprocess
import tempfile
import time

from .container.clone_and_exec import _scrub_credentials
from .push_transport import LeaseError, frozen_transport, git as _git


def _prepare(process: subprocess.Popen, errors) -> None:
    deadline = time.monotonic() + 30
    response = b""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while response != b"start: ok\nprepare: ok\n":
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise LeaseError("Timed out locking the push source")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                errors.seek(0)
                detail = errors.read().decode(errors="replace").strip()
                raise LeaseError(_scrub_credentials(detail) or
                                 "Could not lock the push source")
            response += chunk
            if not b"start: ok\nprepare: ok\n".startswith(response):
                raise LeaseError("Unexpected response while locking the push source")


@dataclass(frozen=True)
class PushLease:
    """One branch destination and the caller's exact expected remote OID."""

    destination: str
    expected: str

    def __post_init__(self):
        if (not self.destination.startswith("refs/heads/") or
                not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.expected) or
                set(self.expected) == {"0"}):
            raise LeaseError("Lease requires refs/heads/<branch> and a full, "
                             "nonzero lowercase commit OID")
        _git("check-ref-format", self.destination)

    @contextmanager
    def pin_source(self, remote: str):
        """Hold Git's HEAD and branch locks through checks, push and receipt."""
        if not remote or remote.startswith("-") or any(c.isspace() for c in remote):
            raise LeaseError("Lease remote must be one unambiguous remote name or URL")
        branch = _git("symbolic-ref", "--quiet", "HEAD")
        if not branch.startswith("refs/heads/"):
            raise LeaseError("Lease source must be the checked-out local branch")
        oid = _git("rev-parse", "--verify", "HEAD^{commit}")
        if len(self.expected) != len(oid):
            raise LeaseError("Lease OID must use this repository's object format")
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["git", "update-ref", "--stdin"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=errors,
            )
            try:
                process.stdin.write(f"start\nverify HEAD {oid}\nprepare\n".encode())
                process.stdin.flush()
                _prepare(process, errors)
                source = PinnedSource(self, remote, branch, oid, process)
                source.check()
                yield source
            finally:
                # EOF aborts the verification transaction; it never writes refs.
                with suppress(BrokenPipeError, OSError):
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()


@dataclass(frozen=True)
class PinnedSource:
    """The committed source covered by the gate, with a live Git transaction."""

    lease: PushLease
    remote: str
    branch: str
    oid: str
    process: subprocess.Popen

    @property
    def refspec(self) -> str:
        return f"{self.oid}:{self.lease.destination}"

    def check(self) -> None:
        """Refuse a lost lock, moved source, or uncommitted test input."""
        if self.process.poll() is not None:
            raise LeaseError("Push source lock was lost; run the gate again")
        if (_git("symbolic-ref", "--quiet", "HEAD") != self.branch or
                _git("rev-parse", "--verify", "HEAD^{commit}") != self.oid):
            raise LeaseError("Push source changed during the gate")
        if _git("status", "--porcelain", "--untracked-files=all",
                "--ignore-submodules=none"):
            raise LeaseError("Leased pushes require a clean index and working tree")

    @contextmanager
    def push_command(self, authorized_urls: tuple[str, ...], by_url: bool):
        """Keep the authorized transport snapshot alive through push and receipt."""
        if len(authorized_urls) != 1:
            raise LeaseError("Leased pushes require exactly one push URL")
        if not by_url and _git("config", "--bool", "--get", f"remote.{self.remote}.mirror",
                               allowed=(0, 1)) == "true":
            raise LeaseError("Leased pushes do not support mirror remotes")
        with frozen_transport(self.remote, authorized_urls[0], by_url) as env:
            advertised = _git("ls-remote", "--symref", "--", self.remote,
                              self.lease.destination, env=env)
            if any(line.startswith("ref: ") for line in advertised.splitlines()):
                raise LeaseError("Leased pushes do not support symbolic remote branches")
            self.check()
            yield (["git", "push", "--no-follow-tags", "--recurse-submodules=no",
                    f"--force-with-lease={self.lease.destination}:{self.lease.expected}",
                    "--", self.remote, self.refspec], env)
