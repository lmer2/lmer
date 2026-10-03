"""Guards for lmer's browser-automation substrate (issue #410).

lmer used to pre-wire an `@playwright/mcp` MCP server and pre-approve
`mcp__playwright__*`. That server was never the chosen substrate. It cost
tool schema in every session that loaded it, and it only ever loaded for lmer
self-development: a session's project is its working directory, `/workspace`,
so the project `.mcp.json` Claude Code reads is the target repo's — this
repo's file only counted when `/workspace` *was* the lmer checkout. It is
gone; browser work goes through the `chrome-agent` skill (captivus/chrome-agent
over CDP) instead.

These are source-level guards. They cannot prove the image builds, but they
catch the wiring silently coming back or silently going away — which is the
failure mode that would otherwise surface as a session that cannot launch a
browser, long after the change that caused it.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_JSON = REPO_ROOT / ".mcp.json"
SETTINGS_JSON = REPO_ROOT / "agent-files" / "claude" / "settings.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
SKILL = REPO_ROOT / "agent-files" / "claude" / "skills" / "chrome-agent" / "SKILL.md"


def _shim_script(containerfile: str) -> str:
    """Lift the chromium-shim RUN body out of the Containerfile.

    Testing the shell rather than the text is the point: this repo's CI does
    not build the Containerfile on merge requests (only on tags and the
    default branch), so the resolution logic is the one part of this change
    a pipeline would never exercise.
    """
    lines = containerfile.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("RUN set -eu;") and "ms-playwright" in "\n".join(
            lines[i : i + 8]
        )
    )
    body: list[str] = []
    for line in lines[start:]:
        body.append(line[4:] if not body else line)
        if not line.rstrip().endswith("\\"):
            break
    return "\n".join(body)


def _run_shim(containerfile: str, cache: Path, link: Path):
    """Run the shim's own shell against a fixture cache, under `sh`.

    `sh` rather than bash: that is the default shell a container build gives a
    RUN instruction, so a bashism here would pass in the test and fail in the
    build. The shim resolves an absolute cache path and writes an absolute
    link; both are redirected at the fixture tree so the test never touches
    /usr/bin.
    """
    script = _shim_script(containerfile)
    script = script.replace("/home/developer/.cache/ms-playwright", str(cache))
    script = script.replace("/usr/bin/chromium", str(link))
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=60
    )


def _plant_chromium(cache: Path, build: str) -> Path:
    binary = cache / build / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture(scope="module")
def mcp_config() -> dict:
    return json.loads(MCP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings() -> dict:
    return json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def containerfile() -> str:
    return CONTAINERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill() -> str:
    assert SKILL.is_file(), f"{SKILL} is missing"
    return SKILL.read_text(encoding="utf-8")


class TestPlaywrightMcpIsUnwired:
    def test_mcp_json_wires_no_playwright_server(self, mcp_config):
        servers = mcp_config["mcpServers"]
        assert not any("playwright" in name.lower() for name in servers), (
            f"a playwright MCP server is wired again in {MCP_JSON.name}: "
            f"{sorted(servers)}"
        )

    def test_mcp_json_wires_no_server_at_all(self, mcp_config):
        """lmer ships no MCP server of its own.

        The file stays present and valid: it is the seam a personal
        .mcp.local.json merges into at container start (libexec/claude-runner.sh),
        and that merge needs a base file with an mcpServers object.
        """
        assert mcp_config == {"mcpServers": {}}, (
            f"{MCP_JSON.name} should be an empty-but-present MCP config; "
            "adding a server here loads it into every self-dev session"
        )

    def test_settings_pre_approve_no_playwright_mcp_tools(self, settings):
        allow = settings["permissions"]["allow"]
        offenders = [entry for entry in allow if "playwright" in entry.lower()]
        assert not offenders, (
            f"{SETTINGS_JSON.name} pre-approves playwright MCP tools again: {offenders}"
        )

    def test_settings_pre_approve_no_mcp_tools_at_all(self, settings):
        """No MCP server is wired, so no mcp__ grant can be earning anything."""
        allow = settings["permissions"]["allow"]
        offenders = [entry for entry in allow if entry.startswith("mcp__")]
        assert not offenders, (
            f"{SETTINGS_JSON.name} pre-approves MCP tools for servers lmer does not "
            f"wire: {offenders}"
        )


class TestChromeAgentIsWired:
    def test_settings_allow_the_chrome_agent_cli(self, settings):
        allow = settings["permissions"]["allow"]
        assert "Bash(chrome-agent:*)" in allow
        assert "Skill(chrome-agent)" in allow

    def test_settings_allow_the_launch_wrapper_by_its_own_name(self, settings):
        """`chrome-agent-launch` is a different command from `chrome-agent`.

        The skill tells the agent to launch through the wrapper; a session that
        stops on a permission prompt there will fall back to the bare launch,
        which is exactly the unattributable browser the sweep must skip.
        """
        assert "Bash(chrome-agent-launch:*)" in settings["permissions"]["allow"]

    def test_settings_allow_xvfb_for_the_headed_path(self, settings):
        """The skill documents a headed recipe; it must not stop on a prompt."""
        assert "Bash(Xvfb:*)" in settings["permissions"]["allow"]

    def test_containerfile_installs_a_pinned_captivus(self, containerfile):
        match = re.search(
            r"uv tool install[^\n]*\bchrome-agent==(\d+\.\d+\.\d+)", containerfile
        )
        assert match, (
            "Containerfile must install captivus chrome-agent at an exact pin — "
            "it is a 0.x one-maintainer project and the CDP verbs the skill "
            "teaches are what moves between minors"
        )

    def test_chromium_shim_hardcodes_no_playwright_build_directory(
        self, containerfile
    ):
        """A hardcoded chromium-<build> breaks silently on a Playwright bump.

        It would still build; the symlink would simply dangle, and the failure
        would surface as a BrowserNotFoundError inside somebody's session.
        """
        hardcoded = re.search(r"ms-playwright/chromium-\d+", containerfile)
        assert not hardcoded, (
            "the Playwright chromium build directory is hardcoded; resolve it "
            "so a browser bump fails the build instead of dangling the symlink"
        )

    def test_the_root_shim_layer_drops_back_to_developer(self, containerfile):
        """The shim needs root; the image must not keep it.

        This change introduced the first `USER root` after the build switches
        to `developer`, so "does the file end as non-root" stopped being
        trivially true. test_containerfile.py only asserts that the string
        `USER developer` appears somewhere, which a file ending as root would
        still satisfy.
        """
        users = [
            line.split(None, 1)[1].strip()
            for line in containerfile.splitlines()
            if line.startswith("USER ")
        ]
        assert users, "the Containerfile sets no USER at all"
        assert users[-1] == "developer", (
            f"the image would run as {users[-1]}; USER directives in order: {users}"
        )

    def test_containerfile_installs_the_headed_x_server(self, containerfile):
        for package in (
            "xorg-x11-server-Xvfb",
            "xorg-x11-xauth",
            "xorg-x11-server-common",
            "libXfont2",
            "libXdmcp",
            "xdotool",
        ):
            assert package in containerfile, (
                f"{package} missing: headed browsing needs the X server the "
                "chromium GUI stack in this image does not include"
            )

    def test_containerfile_keeps_playwright_itself(self, containerfile):
        """Only the MCP wrapper was dropped, not the browser capability.

        The chromium the skill drives is Playwright-managed, so removing the
        install would take the shim's target with it.
        """
        assert "playwright install chromium" in containerfile

    def test_containerfile_bakes_no_playwright_mcp_server(self, containerfile):
        """The server package went with the wiring it served.

        Nothing references it once .mcp.json is empty, and an unused MCP
        package baked into the image is what makes the next reader ask
        whether it is wired.

        Comments are excluded: the layer's own comment names the package it
        no longer installs, which is the point of the comment.
        """
        instructions = "\n".join(
            line
            for line in containerfile.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "@playwright/mcp" not in instructions


class TestTheChromiumShimShell:
    """The shim's own shell, run against fixture trees.

    Everything else about this layer is asserted as text; this is the part
    that has to actually work, and nothing in CI ever runs it.
    """

    def test_one_match_is_symlinked(self, containerfile, tmp_path):
        cache = tmp_path / "ms-playwright"
        binary = _plant_chromium(cache, "chromium-1234")
        link = tmp_path / "chromium"

        proc = _run_shim(containerfile, cache, link)

        assert proc.returncode == 0, proc.stderr
        assert link.is_symlink()
        assert link.resolve() == binary.resolve()

    def test_a_playwright_version_bump_is_followed(self, containerfile, tmp_path):
        """The build directory is resolved, so a bump needs no edit here."""
        cache = tmp_path / "ms-playwright"
        binary = _plant_chromium(cache, "chromium-9999")
        link = tmp_path / "chromium"

        proc = _run_shim(containerfile, cache, link)

        assert proc.returncode == 0, proc.stderr
        assert link.resolve() == binary.resolve()

    def test_no_match_fails_the_build(self, containerfile, tmp_path):
        cache = tmp_path / "ms-playwright"
        cache.mkdir()
        link = tmp_path / "chromium"

        proc = _run_shim(containerfile, cache, link)

        assert proc.returncode != 0, (
            "a missing chromium must fail the build, not dangle a symlink that "
            "surfaces as BrowserNotFoundError inside somebody's session"
        )
        assert "found 0" in proc.stderr
        assert not link.exists()

    def test_several_matches_fail_the_build(self, containerfile, tmp_path):
        """Ambiguity is a human decision, not one to make silently."""
        cache = tmp_path / "ms-playwright"
        _plant_chromium(cache, "chromium-1234")
        _plant_chromium(cache, "chromium-5678")
        link = tmp_path / "chromium"

        proc = _run_shim(containerfile, cache, link)

        assert proc.returncode != 0
        assert "found 2" in proc.stderr
        assert not link.exists()

    def test_the_headless_shell_build_is_not_mistaken_for_chromium(
        self, containerfile, tmp_path
    ):
        """`playwright install chromium` stages two directories, not one.

        `chromium_headless_shell-<build>` sits beside `chromium-<build>` in the
        same cache. If the glob matched it too, every build would fail as
        ambiguous.
        """
        cache = tmp_path / "ms-playwright"
        binary = _plant_chromium(cache, "chromium-1234")
        shell = cache / "chromium_headless_shell-1234" / "chrome-headless-shell-linux64"
        shell.mkdir(parents=True)
        (shell / "chrome-headless-shell").write_text("", encoding="utf-8")
        link = tmp_path / "chromium"

        proc = _run_shim(containerfile, cache, link)

        assert proc.returncode == 0, proc.stderr
        assert link.resolve() == binary.resolve()


class TestChromeAgentSkill:
    def test_skill_has_frontmatter_naming_itself(self, skill):
        assert skill.startswith("---\n")
        frontmatter = skill.split("---\n", 2)[1]
        assert re.search(r"^name: chrome-agent$", frontmatter, re.M), (
            "a skill whose frontmatter has no matching name is dropped silently "
            "by the loader"
        )
        assert re.search(r"^description: \S", frontmatter, re.M)

    def test_skill_carries_the_teardown_hazard(self, skill):
        """Both halves, because either alone is useless.

        A memory figure with no teardown verb does not say what to do, and the
        verb with no figure does not say why it is urgent. Matched loosely:
        the numbers are measurements that may be re-measured.
        """
        assert "chrome-agent stop" in skill
        assert re.search(r"\d{3}\s*MiB", skill), (
            "the skill must quantify the per-instance memory cost — it is why "
            "teardown is not optional"
        )

    def test_skill_carries_the_full_ax_tree_hazard(self, skill):
        """The one call that can blow a context window in a single response."""
        assert "getFullAXTree" in skill
        assert "innerText" in skill

    def test_skill_teaches_the_escalation_ladder(self, skill):
        """The methodology, not a verb list — the reason this is a skill.

        Rung 2 is the load-bearing one: without `isTrusted` and the `Input`
        domain, a silently-dropped click reads as a broken selector and the
        session debugs the wrong thing.
        """
        assert "isTrusted" in skill
        assert "Input.dispatchMouseEvent" in skill
        for rung in ("Rung 1", "Rung 2", "Rung 3"):
            assert rung in skill

    def test_skill_documents_the_optional_headed_path(self, skill):
        assert "Xvfb" in skill
        assert "--headless" in skill

    def test_skill_warns_about_the_name_collision(self, skill):
        """Two projects install an executable called `chrome-agent`."""
        assert "sderosiaux" in skill


class TestSessionEndSweepIsWired:
    def _sweep_entries(self, settings) -> list[dict]:
        return [
            entry
            for entry in settings["hooks"]["SessionEnd"]
            if any("chrome_agent_sweep.sh" in h["command"] for h in entry["hooks"])
        ]

    def test_session_end_runs_the_chrome_agent_sweep(self, settings):
        entries = self._sweep_entries(settings)
        assert entries, (
            "SessionEnd must sweep the browsers a session launched: a forgotten "
            "instance is a few hundred MiB nobody reclaims"
        )
        for entry in entries:
            for hook in entry["hooks"]:
                if "chrome_agent_sweep.sh" in hook["command"]:
                    assert "|| true" in hook["command"], (
                        "the sweep must never fail session end"
                    )

    def test_the_sweep_does_not_run_when_the_session_continues(self, settings):
        """`clear` and `resume` fire SessionEnd on a session that carries on.

        Sweeping there stops a browser still in use, so the hook is matched to
        the terminal reasons only. Without the matcher the sweep's own payload
        check is the last line of defence, which is one line too few.
        """
        for entry in self._sweep_entries(settings):
            matcher = entry.get("matcher")
            assert matcher, (
                "the sweep needs a SessionEnd matcher; unmatched it also fires "
                "for `clear` and `resume`, where the session keeps running"
            )
            assert "clear" not in matcher
            assert "resume" not in matcher

    def test_work_session_end_still_runs_for_every_reason(self, settings):
        """Splitting the array must not narrow the run-state push."""
        unmatched = [
            entry
            for entry in settings["hooks"]["SessionEnd"]
            if not entry.get("matcher")
            and any("work session-end" in h["command"] for h in entry["hooks"])
        ]
        assert unmatched, (
            "`work session-end` must fire however the session ended — it is what "
            "releases the run claim"
        )
