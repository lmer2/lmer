"""Tests for one run's file listing and its forge links (issue #141, T66).

The operator asked: "the detail view should have a list of run files, clicking takes
the user to the work repo (gitlab, github etc.,) to view the file". The run dir
holds the whole record of a run — spec, goals, plan, ledger, events, log, retro,
reports — and the UI showed none of it.

The properties pinned here, in the order they matter:

- **a tokenised work-repo URL never reaches the payload.** The configured URL is
  routinely tokenised (``LMER_WORK_REPO`` normally is) and what this route returns
  is rendered as a clickable link in a browser, which is a URL somebody copies. So
  the base goes through the same ``_web_base_from_remote`` scrub the container-side
  links use, and the test below reads the *whole* serialised body rather than the
  fields it expects the token in
- the files are read off the mirror's disk, not from a list of the names a run is
  *supposed* to write — a hardcoded list would hide the file somebody came to read
- GitLab's ``/-/blob/`` and GitHub's ``/blob/`` both come out right, and a host
  detection cannot classify — ``git.<domain>``, which is what a self-hosted GitLab
  is usually called — gets GitLab's shape, the same default the container side has
  applied since #104. T66 built no URL there, which meant the same run dir was
  linked in ``work`` and unlinked here; T86 made the two agree and moved the
  no-link answer to ``work_repo_forge``, the operator's own setting
- that setting beats detection in both directions and switches links off entirely
  when it reads ``none``
- a run absent from the mirror is a state (nothing pushed yet), not an error
- it is a route of its own rather than fields on the fleet payload, so the poll
  that feeds every row does not grow a directory walk per run

Real directories in ``tmp_path`` throughout, and a real ``git init`` where the
branch in a URL is under test — the branch comes from the mirror's HEAD, and a
mocked one would pass while the actual command line drifted.
"""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import store
from tests.conftest import strip_lmer_env

# The panel parser, imported rather than copied — a second one would drift from
# the component it reads (the same trade that file makes with
# ``tests.test_platform_web_app``). The UI assertions live down here and not there
# because what they pin is T66's placement decision, not the tab layout.
from tests.test_platform_web_details_tabs import RUN_DETAIL, _function_body, _panels

SECRET = "test-secret-value"
TOKEN = "glpat-supersecrettoken12345678"
TOKENISED_URL = f"https://oauth2:{TOKEN}@gitlab.example.com/agents/work.git"
CLEAN_BASE = "https://gitlab.example.com/agents/work"

HOST = "gitlab.example.com"
PROJECT = "agents/global"
SLUG = "develop-issue-1"
REL = f"{HOST}/{PROJECT}/runs/{SLUG}"

#: What a run dir actually holds, so the listing is checked against files rather
#: than against the fixture's own idea of them.
RUN_FILES = (
    "spec.md", "goals.md", "plan.md", "plan.index.json", "ledger.yaml",
    "state.yaml", "events.jsonl", "log.yaml", "retro.md",
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_WORK_REPO_MIRROR, cfg.ENV_WORK_REPO_FORGE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def mirror(tmp_path):
    """A mirror shaped like the daemon's: one run dir, a git checkout on `main`."""
    root = tmp_path / "mirror"
    run_dir = root / HOST / PROJECT / "runs" / SLUG
    run_dir.mkdir(parents=True)
    for name in RUN_FILES:
        (run_dir / name).write_text("x\n", encoding="utf-8")
    (run_dir / "reports").mkdir()
    (run_dir / "reports" / "260726-10-00-00.md").write_text("# r\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    return root


def _config(mirror, url=TOKENISED_URL, forge=None):
    return cfg.load(
        {
            "work_repo_url": url,
            "work_repo_mirror": str(mirror),
            "work_repo_forge": forge,
        }
    )


def _client(config):
    def fake_state(config, *, force_pull=False):
        return {"runs": [], "attention": []}

    return TestClient(api.create_app(config, SECRET, state_builder=fake_state))


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def _files(client, host=HOST, project=PROJECT, slug=SLUG):
    return client.get(
        "/api/runs/files",
        params={"host": host, "project": project, "slug": slug},
        headers=bearer_header(),
    )


# --- the credential scrub ----------------------------------------------------

def test_a_tokenised_work_repo_url_never_reaches_the_payload(platform_root, mirror):
    """The load-bearing property. Every url here is rendered as a link a human can
    copy out of a browser, and the configured work-repo URL is normally tokenised —
    so the whole serialised body is searched, not just the fields expected to
    carry it."""
    response = _files(_client(_config(mirror)))

    assert response.status_code == 200
    body = json.dumps(response.json())
    assert TOKEN not in body
    assert "oauth2" not in body
    assert CLEAN_BASE in body, "the scrub took the whole URL with it"


def test_the_urls_are_built_from_the_scrubbed_base(platform_root, mirror):
    payload = _files(_client(_config(mirror))).json()
    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}

    assert by_name["spec.md"] == f"{CLEAN_BASE}/-/blob/main/{REL}/spec.md"
    assert payload["run_dir_url"] == f"{CLEAN_BASE}/-/tree/main/{REL}"


def test_an_ssh_work_repo_url_is_normalised_like_the_container_side(
    platform_root, mirror
):
    config = _config(mirror, url="git@gitlab.example.com:agents/work.git")
    payload = _files(_client(config)).json()

    assert payload["run_dir_url"] == f"{CLEAN_BASE}/-/tree/main/{REL}"


# --- what is listed ----------------------------------------------------------

def test_the_files_come_off_disk_and_are_sorted(platform_root, mirror):
    payload = _files(_client(_config(mirror))).json()
    names = [entry["name"] for entry in payload["files"]]

    assert names == sorted(names)
    assert set(RUN_FILES) <= set(names), f"missing run files: {names}"
    assert payload["present"] is True
    assert payload["rel_path"] == REL
    assert payload["truncated"] is False


def test_a_file_a_taskdef_invented_is_listed_too(platform_root, mirror):
    """Read off disk, never from a list of the names a run *should* hold: what a
    taskdef writes changes with the taskdef."""
    (mirror / HOST / PROJECT / "runs" / SLUG / "handover.md").write_text(
        "x\n", encoding="utf-8"
    )
    payload = _files(_client(_config(mirror))).json()

    assert "handover.md" in [entry["name"] for entry in payload["files"]]


def test_reports_are_listed_under_their_directory(platform_root, mirror):
    payload = _files(_client(_config(mirror))).json()
    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}

    assert by_name["reports/260726-10-00-00.md"] == (
        f"{CLEAN_BASE}/-/blob/main/{REL}/reports/260726-10-00-00.md"
    )


def test_dotfiles_are_not_listed(platform_root, mirror):
    """Nothing a run writes starts with a dot, so anything that does belongs to git
    or an editor."""
    run_dir = mirror / HOST / PROJECT / "runs" / SLUG
    (run_dir / ".gitkeep").write_text("", encoding="utf-8")
    (run_dir / ".cache").mkdir()
    (run_dir / ".cache" / "junk.json").write_text("{}", encoding="utf-8")

    names = [entry["name"] for entry in _files(_client(_config(mirror))).json()["files"]]

    assert not [name for name in names if "." == name[0] or "/." in name], names


def test_directories_are_not_listed_as_files(platform_root, mirror):
    names = [entry["name"] for entry in _files(_client(_config(mirror))).json()["files"]]

    assert "reports" not in names


def test_a_long_run_is_truncated_and_says_so(platform_root, mirror):
    reports = mirror / HOST / PROJECT / "runs" / SLUG / "reports"
    for index in range(api._MAX_RUN_FILES + 5):
        (reports / f"r{index:04d}.md").write_text("x\n", encoding="utf-8")

    payload = _files(_client(_config(mirror))).json()

    assert payload["truncated"] is True
    assert len(payload["files"]) == api._MAX_RUN_FILES
    assert payload["run_dir_url"], "a truncated list still has to link the directory"


# --- forges ------------------------------------------------------------------

def test_a_github_work_repo_gets_githubs_path_shape(platform_root, mirror):
    config = _config(mirror, url=f"https://x-access-token:{TOKEN}@github.com/o/work.git")
    payload = _files(_client(config)).json()
    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}

    assert by_name["spec.md"] == f"https://github.com/o/work/blob/main/{REL}/spec.md"
    assert payload["run_dir_url"] == f"https://github.com/o/work/tree/main/{REL}"
    assert TOKEN not in json.dumps(payload)


def test_a_self_hosted_gitlab_on_a_plain_hostname_gets_links(platform_root, mirror):
    """T86, and the bug is the *inconsistency* T66 left: `git.<domain>` matches no
    detection rule, so this listing built no URL for any run — while the container
    side's `web_url_for` linked the very same run dir, because it passes GitLab as
    its default. One behaviour on both surfaces, and the primary deployed work repo
    is a self-hosted GitLab on exactly such a name."""
    config = _config(mirror, url="https://git.example.com/agents/work.git")
    payload = _files(_client(config)).json()

    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}
    assert by_name["spec.md"] == (
        f"https://git.example.com/agents/work/-/blob/main/{REL}/spec.md"
    )
    assert payload["run_dir_url"] == (
        f"https://git.example.com/agents/work/-/tree/main/{REL}"
    )


def test_the_forge_knob_set_to_none_lists_the_files_with_no_url(platform_root, mirror):
    """T66's honest-None outcome, now where it belongs: an operator whose work repo
    is neither forge says so once, and every file comes back a plain name. This is
    the only way to get unlinked chips, which is why the opt-out is a setting and
    not the absence of one."""
    config = _config(
        mirror, url="https://git.example.com/agents/work.git",
        forge=cfg.WORK_REPO_FORGE_NONE,
    )
    payload = _files(_client(config)).json()

    assert [entry["name"] for entry in payload["files"]], "the names went with the URL"
    assert all(entry["url"] is None for entry in payload["files"])
    assert payload["run_dir_url"] is None
    assert payload["present"] is True, "the files are still there"


def test_the_forge_knob_beats_detection_on_a_host_that_reads_as_gitlab(
    platform_root, mirror
):
    """`none` is not the only override that has to win: a host called
    gitlab.<domain> that is really something else is the operator's to correct."""
    config = _config(mirror, forge="github")
    payload = _files(_client(config)).json()
    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}

    assert by_name["spec.md"] == f"{CLEAN_BASE}/blob/main/{REL}/spec.md"
    assert payload["run_dir_url"] == f"{CLEAN_BASE}/tree/main/{REL}"


def test_the_forge_knob_covers_github_enterprise_on_a_custom_hostname(
    platform_root, mirror
):
    """The other gap detection cannot close (`_is_github_host` says so itself): a
    GitHub Enterprise Server sits on any name, and without the knob it would get
    GitLab's `/-/blob/` by default."""
    config = _config(mirror, url="https://code.acme.internal/o/work.git", forge="github")
    payload = _files(_client(config)).json()
    by_name = {entry["name"]: entry["url"] for entry in payload["files"]}

    assert by_name["spec.md"] == (
        f"https://code.acme.internal/o/work/blob/main/{REL}/spec.md"
    )


def test_detection_still_decides_when_the_knob_is_unset(platform_root, mirror):
    """The knob is an override, not a replacement: gitlab.com and github.com are
    still read off the host, so nobody has to configure the ordinary cases."""
    gitlab = _files(_client(_config(mirror, url="https://gitlab.com/g/work.git"))).json()
    github = _files(_client(_config(mirror, url="https://github.com/o/work.git"))).json()

    assert gitlab["run_dir_url"] == f"https://gitlab.com/g/work/-/tree/main/{REL}"
    assert github["run_dir_url"] == f"https://github.com/o/work/tree/main/{REL}"


def test_no_work_repo_configured_still_lists_the_files(platform_root, mirror):
    config = cfg.load({"work_repo_mirror": str(mirror)})
    payload = _files(_client(config)).json()

    assert [entry["name"] for entry in payload["files"]]
    assert all(entry["url"] is None for entry in payload["files"])


def test_the_branch_comes_from_the_mirrors_checkout(platform_root, mirror):
    """The mirror is reset onto whatever the remote's default branch is, so the ref
    in a link is read rather than assumed."""
    _git(mirror, "checkout", "-q", "-b", "trunk")
    payload = _files(_client(_config(mirror))).json()

    assert payload["run_dir_url"] == f"{CLEAN_BASE}/-/tree/trunk/{REL}"


def test_a_mirror_that_is_not_a_git_checkout_falls_back_to_main(
    platform_root, tmp_path
):
    root = tmp_path / "bare-mirror"
    run_dir = root / HOST / PROJECT / "runs" / SLUG
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    payload = _files(_client(_config(root))).json()

    assert payload["run_dir_url"] == f"{CLEAN_BASE}/-/tree/main/{REL}"


# --- absence, refusals, auth -------------------------------------------------

def test_a_run_absent_from_the_mirror_is_a_state_not_an_error(platform_root, mirror):
    response = _files(_client(_config(mirror)), slug="never-pushed")

    assert response.status_code == 200
    payload = response.json()
    assert payload["present"] is False
    assert payload["files"] == []
    assert payload["run_dir_url"] is None
    assert payload["rel_path"] is None
    assert payload["run"] == {"host": HOST, "project": PROJECT, "slug": "never-pushed"}


def test_a_named_runs_files_are_listed_under_the_directory_it_was_renamed_into(
    platform_root, mirror
):
    """The second half of the field report (T90): this route answered
    ``present: false`` for a run whose files were right there on disk.

    The run was *named*, so the container had taken its one rename to
    ``runs/<slug>--<name>`` and the address the platform composed from the slug no
    longer existed. Asked for by slug — which is what the row carries, and what the
    run's identity is — the answer now names the directory a human can open, while
    ``run`` still identifies the run by its slug.
    """
    named = mirror / HOST / PROJECT / "runs" / f"{SLUG}--the-name"
    named.mkdir(parents=True)
    (named / "state.yaml").write_text(f"schema: 1\nslug: {SLUG}\n", encoding="utf-8")
    (named / "plan.md").write_text("# plan\n", encoding="utf-8")
    shutil.rmtree(mirror / HOST / PROJECT / "runs" / SLUG)

    payload = _files(_client(_config(mirror))).json()

    assert payload["present"] is True
    assert payload["run"] == {"host": HOST, "project": PROJECT, "slug": SLUG}
    assert payload["rel_path"] == f"{HOST}/{PROJECT}/runs/{SLUG}--the-name"
    assert [entry["name"] for entry in payload["files"]] == ["plan.md", "state.yaml"]
    assert payload["files"][0]["url"].endswith(f"runs/{SLUG}--the-name/plan.md")


def test_the_note_says_why_the_links_cannot_point_at_unpushed_content(
    platform_root, mirror
):
    """The one good property of a force-reset mirror, stated where a reader of the
    API sees it: everything in it is by definition pushed."""
    payload = _files(_client(_config(mirror))).json()

    assert "force-reset" in payload["note"]
    assert "pushed" in payload["note"]


def test_a_missing_field_is_refused_by_name(platform_root, mirror):
    response = _files(_client(_config(mirror)), project="")

    assert response.status_code == 400
    assert "project" in response.json()["detail"]


def test_the_route_needs_the_shared_secret(platform_root, mirror):
    response = _client(_config(mirror)).get(
        "/api/runs/files", params={"host": HOST, "project": PROJECT, "slug": SLUG}
    )

    assert response.status_code == 401


def test_the_route_is_listed_in_the_api_index(platform_root, mirror):
    """The plain-text index is how an operator with a terminal finds the verbs."""
    body = _client(_config(mirror)).get("/api", headers=bearer_header()).text

    assert "GET  /api/runs/files" in body
    assert "null url" in body, "the entry has to say what a missing url means"


def test_reading_one_runs_files_does_not_pull_the_mirror(
    platform_root, mirror, monkeypatch
):
    """A detail view must not put a remote's latency in front of opening a page —
    the fleet poll is what keeps the mirror current."""
    pulls = []
    monkeypatch.setattr(api, "pull", lambda *a, **k: pulls.append(a))

    assert _files(_client(_config(mirror))).status_code == 200
    assert pulls == []


def test_the_fleet_payload_did_not_grow_a_file_list(platform_root, mirror):
    """The reason this is a route: /api/state carries every tracked run and is
    polled on a timer, so a listing there would cost a walk per run per poll."""
    from lmer_platform import inventory

    fields = set(inventory.RunView.__dataclass_fields__)
    assert not {"files", "run_files"} & fields, sorted(fields)


# --- where it is in the UI ---------------------------------------------------

def test_the_file_list_is_in_the_overview_panel_rather_than_a_fifth_tab():
    """Placement, and the reasoning is the operator's own: they are "not interested in
    overview or meta tabs most of the time (as long as i know the taskdef, target,
    and repo name)", which makes a fifth row of tabs the wrong answer and the
    overview a defensible one — this is the panel whose first row is the run path
    the list resolves against."""
    panels = _panels("tab")
    detail = RUN_DETAIL.read_text(encoding="utf-8")

    assert set(panels) == {"overview", "meta", "lmer", "exit"}, (
        f"the file list grew a tab of its own: {sorted(panels)}"
    )
    assert "run files" in panels["overview"]
    assert detail.count('<div class="section-title">run files</div>') == 1


def test_the_file_list_is_last_in_the_overview_and_leaves_the_views_bottom_free():
    """The record, not the news: the facts and the verb that acts on them stay
    above it. And it is inside the tab window, because the always-visible slot at
    the bottom of the details view is what related runs (T53) will want."""
    overview = _panels("tab")["overview"]
    detail = RUN_DETAIL.read_text(encoding="utf-8")

    assert overview.index("run files") > overview.index("continuing this run")
    assert overview.index("run files") > overview.index("recent events")

    below_the_tabs = detail[detail.rindex("</v-tabs-window>"):]
    assert "run files" not in below_the_tabs


def test_the_list_is_read_when_the_overview_opens_not_with_every_fleet_poll():
    """One read per visit. The fleet payload is polled on a timer and feeds every
    row; this is fetched by the one view that renders it."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    loader = _function_body(detail, "async function loadRunFiles")

    assert detail.count("api/runs/files") == 1, "the listing is fetched more than once"
    assert "api/runs/files" in loader
    assert "if (runFilesRequested) return" in loader, "the read is not guarded"
    assert re.search(r"if \(value === 'overview'\) loadRunFiles\(\)", detail), (
        "nothing arms the read when the overview panel is opened"
    )


def test_the_unlinked_chips_hint_names_a_reason_that_can_still_happen():
    """T86 changed which states this tooltip can appear in. It used to say the run's
    host "is not a forge this platform knows how to build browse URLs for" — which
    is what the operator read when every chip came back unlinked, and it was both
    the wrong host (the links point at the *work* repo) and no longer the reason.
    What is left is the operator's own settings, so that is what it says."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    start = detail.index("const unlinkedFileHint")
    hint = detail[start:detail.index("\n\n", start)]

    assert "work_repo_forge=none" in hint, "the opt-out is not named"
    assert "not a forge" not in hint, "an unclassified host is no longer why"
    assert "run.host" not in hint, "the run's host is not the work repo's host"


def test_a_file_the_daemon_could_not_link_is_rendered_without_a_link():
    """The UI half of the no-guessed-URL rule: a null url is a name, never an
    anchor pointing at a path shape nobody knows."""
    overview = _panels("tab")["overview"]
    chips = re.findall(r"<v-chip[^>]*>\{\{ file\.name \}\}", overview, re.S)

    assert len(chips) == 2, f"expected a linked and an unlinked chip, got {len(chips)}"
    linked, unlinked = chips
    assert 'v-if="file.url"' in linked and ':href="file.url"' in linked
    assert "v-else" in unlinked and ":href" not in unlinked
