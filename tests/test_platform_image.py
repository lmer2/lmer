"""Source guards for the platform container image (``Dockerfile.platform``, #150).

Nothing here builds anything. A build needs a daemon, a network and minutes, and
CI builds the image on its own; what these tests pin are the properties a
successful build cannot tell you it lost — that the Node the bundle is built with
is still the version ``lmer platform setup-ui`` pins, that the environment seam
points at the path the bundle is actually copied to, that the entrypoint names a
subcommand the daemon has, and that the image stays non-root with the client it
needs for the host's runtime.

Read with regexes rather than whole-line equality throughout, so reformatting the
Dockerfile — a continuation moved, an ``apt-get`` argument reordered — fails a
test about formatting and not one about behavior.
"""

import re
from pathlib import Path

import pytest

from lmer_platform import daemon, ui_build

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile.platform"
DOCKERIGNORE = Path(__file__).parent.parent / ".dockerignore"


@pytest.fixture(scope="module")
def content() -> str:
    assert DOCKERFILE.is_file(), f"{DOCKERFILE} is missing"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def commands(content) -> str:
    """*content* with line continuations collapsed, so one RUN is one line.

    Every ``apt-get`` in this image spans several lines, and a test about what is
    installed should not also be a test about where the backslashes fell.
    """
    return re.sub(r"\\\n\s*", " ", content)


# --- the toolchain the bundle is built with ----------------------------------

def test_the_builder_node_matches_the_pinned_toolchain(content):
    """A bundle built by another Node is not the artifact the pin describes.

    ``NODE_VERSION``/``NODE_CHECKSUMS`` are reviewed as a pair (ui_build), and the
    image build cannot verify a checksum — it takes the official Node image — so
    the version literal agreeing with the pin is the whole of that guarantee here.
    """
    match = re.search(r"^FROM\s+node:(?P<version>[0-9][0-9.]*)\s+AS\s+\S+",
                      content, re.MULTILINE)
    assert match, "no pinned `FROM node:<version> AS <stage>` builder stage"
    assert f"v{match.group('version')}" == ui_build.NODE_VERSION, (
        "the builder stage's Node and ui_build.NODE_VERSION have diverged: "
        f"node:{match.group('version')} vs {ui_build.NODE_VERSION}"
    )
    assert "NODE_VERSION" in content, (
        "the Dockerfile must say the literal tracks ui_build.NODE_VERSION, or "
        "the next bump updates one of the two"
    )


def test_the_ui_is_built_in_the_image_and_not_copied_from_the_host(content):
    """web/dist is not committed (spec D10) and .dockerignore excludes it."""
    assert re.search(r"\bnpm\s+ci\b", content), "dependencies not installed with npm ci"
    assert re.search(r"\bnpm\s+run\s+build\b", content), "the bundle is never built"
    assert not re.search(r"^COPY\s+(?!--from)[^\n]*web/dist", content, re.MULTILINE), (
        "the bundle must come from the builder stage, never from the build context"
    )


def test_the_dockerignore_keeps_the_web_sources_and_drops_their_build_state():
    """The builder needs web/; it must not inherit the host's node_modules or dist.

    Named explicitly in .dockerignore because the root ``node_modules/`` and
    ``dist/`` patterns are anchored at the context root and match nothing below it.

    Comment lines are dropped before splitting: the file explains itself at
    length, and prose split on whitespace would otherwise count as entries — the
    word ``web`` appearing in a sentence would fail the last assertion here.
    """
    ignored = [
        entry
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
        for entry in line.split()
    ]
    assert "web/node_modules/" in ignored
    assert "web/dist/" in ignored
    assert not any(entry.rstrip("/") == "web" for entry in ignored), (
        "excluding web/ would leave the builder stage nothing to build"
    )


# --- the seam the daemon reads -----------------------------------------------

def test_the_seam_points_at_the_path_the_bundle_is_copied_to(content):
    """A variable naming a directory the COPY does not create serves no UI."""
    env = re.search(
        rf"^ENV\s+{re.escape(ui_build.ENV_UI_DIST)}=(?P<path>\S+)",
        content, re.MULTILINE,
    )
    assert env, f"the image does not set {ui_build.ENV_UI_DIST}"
    copied = re.search(
        r"^COPY\s+--from=\S+\s+\S*web/dist\s+(?P<dest>\S+)", content, re.MULTILINE
    )
    assert copied, "the built bundle is never copied out of the builder stage"
    assert env.group("path") == copied.group("dest"), (
        f"{ui_build.ENV_UI_DIST}={env.group('path')} but the bundle lands in "
        f"{copied.group('dest')}"
    )
    assert env.group("path").startswith("/"), "the seam needs an absolute path"


# --- the runtime -------------------------------------------------------------

def test_the_runtime_is_a_pinned_slim_python(content):
    assert re.search(r"^FROM\s+python:3\.12-slim\b", content, re.MULTILINE), (
        "the runtime stage must be python:3.12-slim (pyproject requires >=3.12)"
    )


def test_the_package_is_installed_rather_than_mounted(content):
    """The image is the deployment artifact, so it carries its own build."""
    install = re.search(r"pip install[^\n]*", content)
    assert install, "the project is never installed"
    assert not re.search(r"pip install\s+(-e|--editable)\b", content), (
        "an editable install would serve whatever is mounted over it"
    )
    for needed in ("pyproject.toml", "README.md", "LICENSE"):
        assert re.search(rf"^COPY[^\n]*\b{re.escape(needed)}\b", content, re.MULTILINE), (
            f"{needed} is not copied in, and a non-editable install needs it"
        )
        # And they have to be there to copy: a COPY of a missing file fails the
        # build, so a rename or removal at the repo root is this test's business
        # too — the reason those paths are in the CI job's `changes:` list.
        assert (DOCKERFILE.parent / needed).is_file(), (
            f"{needed} is a COPY target but does not exist at the repo root"
        )
    assert re.search(r"^COPY\s+src/", content, re.MULTILINE), "the sources are not copied in"


def test_git_and_a_docker_client_are_installed(content, commands):
    """git pulls the work-repo mirror; the docker client starts sessions.

    ``procps`` is here for a third reason, weaker but concrete: the spike runbook
    in docs/PLATFORM-CONTAINER.md compares a registry entry's recorded pid against
    ``ps -ef`` inside this container, and python:3.12-slim ships no ``ps``.
    """
    assert re.search(r"apt-get install[^\n]*\bgit\b", commands), "git is not installed"
    assert re.search(r"apt-get install[^\n]*\bprocps\b", commands), (
        "no procps — the runbook's `docker exec lmer-platform ps -ef` step cannot run"
    )
    assert re.search(r"apt-get install[^\n]*\bdocker-ce-cli\b", commands), (
        "no docker client — `lmer` cannot start a session container"
    )
    assert "signed-by=/etc/apt/keyrings/docker.asc" in content, (
        "Docker's repository must be added with its key, not trusted blindly"
    )


def test_the_uid_and_gid_are_build_args_defaulting_to_the_first_host_user(content, commands):
    """The state dir is bind-mounted, so the user inside owns files outside."""
    for arg in ("BUILD_UID", "BUILD_GID"):
        assert re.search(rf"^ARG\s+{arg}=1000\s*$", content, re.MULTILINE), (
            f"{arg} must be a build arg defaulting to 1000"
        )
    assert re.search(r"useradd[^\n]*\$BUILD_UID", commands), (
        "BUILD_UID is declared but never used to create the user"
    )


def test_the_image_runs_as_a_non_root_user(content):
    assert re.search(r"^USER\s+developer\s*$", content, re.MULTILINE), (
        "the platform must not serve as root"
    )
    assert "NOPASSWD" not in content, (
        "unlike a session container this one installs nothing at run time"
    )


def test_build_provenance_is_baked(content):
    """Asking what commit an image is has to be answerable: /opt/lmer has no .git."""
    assert re.search(r"^ARG\s+LMER_BUILD_COMMIT=", content, re.MULTILINE)
    assert re.search(r"^ENV\s+LMER_BUILD_COMMIT=", content, re.MULTILINE)
    assert "/opt/lmer/BUILD_INFO" in content


# --- how it starts -----------------------------------------------------------

def test_the_entrypoint_is_the_platform_daemon(content):
    entrypoint = re.search(r'^ENTRYPOINT\s+\["lmer",\s*"platform"\]', content, re.MULTILINE)
    assert entrypoint, (
        'ENTRYPOINT must be ["lmer", "platform"] so a CMD is read as its subcommand'
    )


def test_the_default_command_is_a_subcommand_the_daemon_has(content):
    """``docker run <image>`` serves; ``docker run <image> status`` still inspects.

    Checked against the parser rather than against the string ``run``, so a
    renamed subcommand fails here instead of at the first container start.
    """
    cmd = re.search(r'^CMD\s+\["(?P<command>[^"]+)"\]', content, re.MULTILINE)
    assert cmd, "no default CMD — `docker run <image>` would print usage"

    command = cmd.group("command")
    try:
        parsed = daemon.build_arg_parser().parse_args([command])
    except SystemExit:  # argparse's answer to an unknown subcommand
        pytest.fail(f"CMD names {command!r}, which `lmer platform` does not accept")
    assert parsed.command == command
    assert command == "run", (
        "the default has to be the serving subcommand, not a diagnostic one"
    )
