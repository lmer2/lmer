"""Tests for .github/scripts/gate-version-reuse.py (release publish gate).

The gate stands between "this run built distributions" and "publish them",
and it is the only thing that makes `skip-existing: true` safe: without it a
version already on the index is skipped silently, whatever it holds. The
properties guarded here are the ones a real release depends on and a
substring assertion against a workflow heredoc cannot see — the index URL
derived from the project url, the 404-vs-other-HTTP branch, the foreign
artifact refusal, and the fail-closed rule that an already-published version
publishes only when the admin-controlled RELEASE_RESUME_VERSION names it.

Everything is hermetic: the index is a stub HTTP server bound to localhost
(the script's PYPI_PROJECT_URL seam points at it), and dist/ is a scratch
directory — no network, no PyPI, no tokens.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "gate-version-reuse.py"

VERSION = "0.5.0"
TAG = f"v{VERSION}"
WHEEL = f"lmer-{VERSION}-py3-none-any.whl"
SDIST = f"lmer-{VERSION}.tar.gz"


def published_payload(filenames):
    """A PyPI JSON API response body for `filenames`."""
    return {
        "info": {"name": "lmer", "version": VERSION},
        "urls": [
            {
                "filename": name,
                "digests": {"sha256": f"{i:064x}"},
                "upload_time_iso_8601": "2026-07-01T10:00:00.000000Z",
                "provenance": f"https://example.invalid/provenance/{name}",
            }
            for i, name in enumerate(filenames)
        ],
    }


class StubIndex:
    """Localhost stand-in for the index JSON API.

    `responses` maps request path -> (status, body). Every requested path is
    recorded so a test can assert what the script derived from
    PYPI_PROJECT_URL rather than trusting the code's own arithmetic.
    """

    def __init__(self, responses):
        self.responses = responses
        self.requested = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
                stub.requested.append(self.path)
                status, body = stub.responses.get(self.path, (404, {}))
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def project_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}/project/lmer/"


@pytest.fixture
def dist(tmp_path):
    """A dist/ holding this run's two distributions."""
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / WHEEL).write_bytes(b"wheel bytes")
    (directory / SDIST).write_bytes(b"sdist bytes")
    return directory


def run_gate(tmp_path, project_url, tag=TAG, resume=None, dist_dir="dist"):
    env = {
        "PATH": "/usr/bin:/bin",
        "PYPI_PROJECT_URL": project_url,
        "GITHUB_REF_NAME": tag,
        "DIST_DIR": dist_dir,
    }
    if resume is not None:
        env["RELEASE_RESUME_VERSION"] = resume
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_script_is_executable_with_a_python_shebang():
    """Invoked as `.github/scripts/gate-version-reuse.py` by the workflow,
    exactly like its sibling verify-tag-signature.sh."""
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "gate script is not executable"
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3\n")


def test_first_publish_proceeds_when_the_version_is_absent(tmp_path, dist):
    """404 from the index is the first-publish case, not an error."""
    with StubIndex({}) as index:  # every path 404s
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not on the index yet" in result.stdout
    assert "::error::" not in result.stdout


def test_index_url_is_derived_from_the_project_url(tmp_path, dist):
    """The JSON API path is derived from PYPI_PROJECT_URL — nothing about
    pypi.org is hard-coded, which is what lets the rehearsal transform point
    the same script at TestPyPI."""
    with StubIndex({}) as index:
        run_gate(tmp_path, index.project_url)
        assert index.requested == [f"/pypi/lmer/{VERSION}/json"]

    # A trailing-slash-less project url derives the same path.
    with StubIndex({}) as index:
        run_gate(tmp_path, index.project_url.rstrip("/"))
        assert index.requested == [f"/pypi/lmer/{VERSION}/json"]


def test_foreign_artifacts_refuse_the_publish(tmp_path, dist):
    """A published file this run did not build means the version already
    holds a different release: refuse before publish."""
    path = f"/pypi/lmer/{VERSION}/json"
    body = published_payload([WHEEL, SDIST, "lmer-0.5.0-py2-none-any.whl"])
    with StubIndex({path: (200, body)}) as index:
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "lmer-0.5.0-py2-none-any.whl" in result.stdout
    assert "diverged release" in result.stdout


def test_foreign_artifacts_refuse_even_when_resume_is_authorized(tmp_path, dist):
    """The resume variable authorizes re-entry, never publishing over a
    version whose contents this run cannot account for."""
    path = f"/pypi/lmer/{VERSION}/json"
    body = published_payload([WHEEL, "lmer-0.5.0-py2-none-any.whl"])
    with StubIndex({path: (200, body)}) as index:
        result = run_gate(tmp_path, index.project_url, resume=VERSION)
    assert result.returncode == 1
    assert "diverged release" in result.stdout


def test_identical_filenames_fail_closed_without_the_resume_variable(tmp_path, dist):
    """The finding this gate was rewritten for: distribution filenames are a
    pure function of (name, version), so a tag re-pointed at different code
    publishes the SAME filenames. Subset comparison therefore cannot see the
    divergence — the gate refuses instead of warning-and-continuing."""
    path = f"/pypi/lmer/{VERSION}/json"
    with StubIndex({path: (200, published_payload([WHEEL, SDIST]))}) as index:
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "already published" in result.stdout
    # The failure names both remedies, including the exact variable value.
    assert "RELEASE_RESUME_VERSION" in result.stdout
    assert f"'{VERSION}'" in result.stdout
    # And it prints what is published vs what was built, for the human.
    assert "provenance=https://example.invalid/provenance/" in result.stdout
    assert "uploaded=2026-07-01T10:00:00.000000Z" in result.stdout
    assert f"  built     {WHEEL}" in result.stdout


@pytest.mark.parametrize("resume", [VERSION, TAG, f"  {VERSION}  "])
def test_resume_variable_authorizes_the_named_version(tmp_path, dist, resume):
    """An admin naming this exact version (with or without the leading `v`,
    whitespace tolerated) turns the refusal into a warning."""
    path = f"/pypi/lmer/{VERSION}/json"
    with StubIndex({path: (200, published_payload([WHEEL, SDIST]))}) as index:
        result = run_gate(tmp_path, index.project_url, resume=resume)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::" in result.stdout
    assert "::error::" not in result.stdout
    # The warning states the residual: PyPI keeps what it already serves.
    assert "skip-existing will no-op" in result.stdout


@pytest.mark.parametrize("resume", ["", "0.5.1", "v0.4.9", "all"])
def test_resume_variable_must_name_this_version(tmp_path, dist, resume):
    """A stale or blanket value authorizes nothing — the admin re-affirms
    per version, which is what makes clearing it afterwards meaningful."""
    path = f"/pypi/lmer/{VERSION}/json"
    with StubIndex({path: (200, published_payload([WHEEL, SDIST]))}) as index:
        result = run_gate(tmp_path, index.project_url, resume=resume)
    assert result.returncode == 1
    assert "::error::" in result.stdout


@pytest.mark.parametrize("status", [403, 500, 503])
def test_non_404_http_errors_fail_closed(tmp_path, dist, status):
    """An index that cannot answer is not an absent version: publishing
    blind is exactly what this gate exists to prevent."""
    path = f"/pypi/lmer/{VERSION}/json"
    with StubIndex({path: (status, {})}) as index:
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert str(status) in result.stdout


def test_unreachable_index_fails_closed(tmp_path, dist):
    """Same rule for a transport failure (DNS, refused connection)."""
    result = run_gate(tmp_path, "http://127.0.0.1:1/project/lmer/")
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "refusing to publish" in result.stdout


def test_missing_dist_directory_fails(tmp_path):
    """Nothing built means the workflow is broken upstream — fail here
    rather than let an empty publish look like a success."""
    with StubIndex({}) as index:
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 1
    assert "nothing was built" in result.stdout


def test_empty_dist_directory_fails(tmp_path):
    (tmp_path / "dist").mkdir()
    with StubIndex({}) as index:
        result = run_gate(tmp_path, index.project_url)
    assert result.returncode == 1
    assert "no distributions" in result.stdout


@pytest.mark.parametrize("project_url", ["", "https://pypi.org/lmer/", "not-a-url"])
def test_unusable_project_url_fails(tmp_path, dist, project_url):
    """The project url is a transform contract (https://<host>/project/<name>/);
    anything else is a workflow bug, not something to guess around."""
    result = run_gate(tmp_path, project_url)
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_missing_tag_fails(tmp_path, dist):
    with StubIndex({}) as index:
        result = run_gate(tmp_path, index.project_url, tag="")
    assert result.returncode == 1
    assert "GITHUB_REF_NAME" in result.stdout
