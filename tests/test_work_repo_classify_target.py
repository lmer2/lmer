"""The target classifier (run-identity spec §2, decision D2): one table, two
copies, and the seams each copy must agree with.

``work_repo.utils.classify_target`` sorts a task target into exactly one of
six classes, and ``lmer_cli/container/clone_and_exec.py`` carries a verbatim
standalone copy (the entrypoint runs before the package is importable). What
is pinned here, in the order the module decides it:

- the §2 table, row by row, against BOTH copies — every target the spec
  names plus the decisions the task recorded in the docstring (GitHub
  ``/commits/<ref>`` is a reference; a credential on a non-HTTP transport is
  ``other``; the indicator constant's shape);
- ``is_work_item`` over the same rows;
- ``repository_of`` (A1-wave0.repository-of): a table of its own over both
  copies — GitLab ``/-/`` URLs with nested groups, a port and userinfo;
  GitHub pull, issues, blob and tree; the list pages; plain clone URLs; the
  ``None`` rows — plus the §2 table again, asserting an answer exactly for a
  repository or a URL-shaped work item or reference, never with a route, a
  query or a credential in it; and both copies of
  ``_derive_repo_url_from_task_target`` over the §2 table, deriving exactly
  when ``repository_of`` answers for a work item or a reference and parsing
  to the same identity;
- the ``special`` row against the handlers' own claims: every string literal
  in the Slack and Matrix permalink test files (their positive AND negative
  fixtures, and everything else those files happen to spell) goes through
  ``SlackThreadTargets.matches`` / ``MatrixThreadTargets.matches`` and both
  copies, asserting ``matches ⇔ special``;
- the ``repository`` row against ``spawn._plain_repo_url_from_target`` over
  spawn's own fixture lists, apart from the deltas §2 states — a guard that
  holds until a later task turns spawn's helper into a thin call;
- the two copies are AST-identical (the hooks/start.py mirror-guard pattern,
  ``tests.conftest.ast_body_lines``), the copy is stdlib-only, and the
  classifier is pure (no filesystem, no environment).
"""

import ast
import inspect
from pathlib import Path
from urllib.parse import urlparse

import pytest

from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url
from lmer_cli.container import clone_and_exec
from lmer_cli.targets import MatrixThreadTargets, SlackThreadTargets
from lmer_platform import spawn
from tests import test_platform_spawn as spawn_fixtures
from tests.conftest import ast_body_lines
from work_repo import utils

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
UTILS_PATH = SRC / "work_repo" / "utils.py"
ENTRYPOINT_PATH = SRC / "lmer_cli" / "container" / "clone_and_exec.py"

#: The canonical definition and its standalone copy; every table row runs
#: against both.
COPIES = [
    pytest.param(utils, id="work_repo.utils"),
    pytest.param(clone_and_exec, id="clone_and_exec"),
]

CLASSES = ("none", "special", "work-item", "reference", "repository", "other")

GL = "https://gitlab.example.com/agents/global"
GH = "https://github.com/owner/repo"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"

#: Spec §2's table, one ``(target, class)`` per row the spec names, in the
#: spec's row order. Comments mark the decisions recorded in the docstring.
TABLE = [
    # none
    (None, "none"),
    ("", "none"),
    ("   ", "none"),
    ("\t\n", "none"),
    # special — Slack: the canonical permalink and the ?thread_ts= form
    ("https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456", "special"),
    (
        "https://myworkspace.slack.com/archives/C0123ABCDEF"
        "/p1700000000000000?thread_ts=1700000000.123456&cid=C0123ABCDEF",
        "special",
    ),
    # special — Matrix: matrix.to with an id, an alias, percent-encoded; Element
    ("https://matrix.to/#/!abc:example.org/$evt?via=example.org", "special"),
    ("https://matrix.to/#/#alias:example.org/$evt", "special"),
    ("https://matrix.to/#/%21abc%3Aexample.org/%24evt", "special"),
    ("https://app.element.io/#/room/!abc:example.org/$evt", "special"),
    ("https://chat.example.org/#/room/%23alias%3Aexample.org/%24e_1", "special"),
    # work-item — every id-bearing indicator, GitLab then GitHub
    (f"{GL}/-/issues/141", "work-item"),
    (f"{GL}/-/work_items/141", "work-item"),
    (f"{GL}/-/merge_requests/7", "work-item"),
    (f"{GL}/-/merge_requests/7/diffs", "work-item"),
    (f"{GL}/-/issues/141?foo=bar", "work-item"),  # row order: before `other`
    (f"{GL}/-/commit/{FULL_SHA}", "work-item"),
    (f"{GL}/-/commit/abc1234", "work-item"),
    (f"{GL}/-/compare/main...feature", "work-item"),
    (f"{GH}/pull/9", "work-item"),
    (f"{GH}/pull/9/files", "work-item"),
    (f"{GH}/pulls/9", "work-item"),
    (f"{GH}/issues/456", "work-item"),
    (f"{GH}/commit/{FULL_SHA}", "work-item"),
    (f"{GH}/commit/abc1234", "work-item"),
    (f"{GH}/compare/main...feature", "work-item"),
    # work-item — the non-URL tail of the table
    (FULL_SHA, "work-item"),
    ("abc1234", "work-item"),
    ("feature/x", "work-item"),
    ("mr-7", "work-item"),
    ("pr-9", "work-item"),
    ("issue-3", "work-item"),
    ("make the flaky test stop", "work-item"),
    ("v1.2.3", "work-item"),
    # reference — GitLab routes and the id-less list pages
    (f"{GL}/-/blob/main/README.md", "reference"),
    (f"{GL}/-/tree/main", "reference"),
    (f"{GL}/-/raw/main/README.md", "reference"),
    (f"{GL}/-/wikis/home", "reference"),
    (f"{GL}/-/commits/main", "reference"),
    (f"{GL}/-/issues", "reference"),
    (f"{GL}/-/issues/?label_name=bug", "reference"),
    (f"{GL}/-/issues/new", "reference"),
    (f"{GL}/-/work_items", "reference"),
    (f"{GL}/-/merge_requests", "reference"),
    # reference — GitHub routes in the owner/repo/<route> position
    (f"{GH}/blob/main/README.md", "reference"),
    (f"{GH}/tree/main", "reference"),
    (f"{GH}/wiki/Home", "reference"),
    (f"{GH}/commits/main", "reference"),  # decision: joins the reference row
    (f"{GH}/issues", "reference"),
    (f"{GH}/pulls", "reference"),
    # repository — forge roots, the deltas, transports, paths
    (GL, "repository"),
    (f"{GL}.git", "repository"),
    (f"{GL}/", "repository"),
    ("https://git.example.com/agents/global", "repository"),  # first label
    ("http://gitlab.com/group/project", "repository"),
    ("https://github.com/a/b", "repository"),
    ("https://github.com/a/b.git", "repository"),
    ("https://github.com/a/b/c.git", "repository"),  # delta: .git tested first
    ("https://example.com/a/b.git", "repository"),  # unknown host, .git suffix
    ("git@gitlab.example.com:agents/global.git", "repository"),
    ("git@github.com:owner/repo", "repository"),
    ("ssh://git@gitlab.example.com/agents/global.git", "repository"),
    ("ssh://git@gitlab.example.com:2222/agents/global.git", "repository"),
    ("git://git.example.com/agents/global.git", "repository"),  # delta: git://
    ("file:///srv/git/global.git", "repository"),  # delta: file:///path
    ("file:///path", "repository"),
    # decision: an HTTP(S) credential is transport material spawn scrubs
    ("https://oauth2:glpat-not-a-real-token-000@gitlab.example.com/agents/global.git", "repository"),
    (".", "repository"),
    ("./x", "repository"),
    ("../x", "repository"),
    ("/abs", "repository"),
    ("/abs/path", "repository"),
    # other — non-forge hosts, forge URLs with a query/fragment/unknown route
    ("https://docs.python.org/3/library/os.html", "other"),
    ("https://docs.example.com/guide/getting-started", "other"),
    (f"{GL}?ref=main", "other"),
    (f"{GL}#readme", "other"),
    (f"{GH}/discussions/5", "other"),
    (f"{GL}/-/pipelines/1691", "other"),
    ("ftp://gitlab.example.com/group/project", "other"),
    ("https://github.com/a/b/c", "other"),  # three segments, no .git
    ("https://github.com/a", "other"),
    ("https://example.com/a/b", "other"),  # unknown host, no .git
    ("https://en.wikipedia.org/wiki/Git", "other"),  # /wiki/ off the forge position
    ("https://docs.example.com/guide/tree/main", "other"),  # /tree/ likewise
    # decision: a credential on a non-HTTP transport is never a repository
    ("ssh://deploy:not-a-real-secret@gitlab.example.com/agents/global.git", "other"),
    ("deploy:not-a-real-secret@gitlab.example.com:agents/global.git", "other"),
    ("deploy@gitlab.example.com:agents/global.git", "other"),  # scp, not git@
    ("git@gitlab.example.com:", "other"),  # scp with no path
]


def _row_id(row):
    target, klass = row
    return f"{klass}:{target!r}"


GL_NESTED = "https://gitlab.example.com/group/subgroup/project"
GL_PORT = "https://gitlab.example.com:8443/group/project"
HOST_TOKEN = "glpat-not-a-real-token-000"
SLACK = "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456"
MATRIX = "https://matrix.to/#/!abc:example.org/$evt?via=example.org"

#: ``repository_of`` (spec §2): ``(url, repository)``. A derived answer is
#: ``<scheme>://<host[:port]>/<project>`` — the path before ``/-/`` on
#: GitLab, ``owner/repo`` on a GitHub-shaped host — with the route, the
#: query, the fragment and any userinfo gone; a repository is itself, as
#: given; everything else is ``None``.
REPOSITORY_OF = [
    # GitLab: the path before /-/, for every work-item route
    (f"{GL}/-/issues/141", GL),
    (f"{GL}/-/work_items/141", GL),
    (f"{GL}/-/merge_requests/7", GL),
    (f"{GL}/-/merge_requests/7/diffs", GL),
    (f"{GL}/-/commit/{FULL_SHA}", GL),
    (f"{GL}/-/compare/main...feature", GL),
    (f"{GL}/-/issues/141?foo=bar", GL),  # the query goes
    (f"{GL}/-/issues/141#note_1", GL),  # so does the fragment
    # GitLab: nested groups, a port, userinfo, the scheme as given
    (f"{GL_NESTED}/-/issues/12", GL_NESTED),
    (f"{GL_NESTED}/-/merge_requests/3/diffs", GL_NESTED),
    (f"{GL_PORT}/-/merge_requests/55", GL_PORT),  # the port is part of the address
    (f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/agents/global/-/issues/1", GL),
    (f"https://user:pw@gitlab.example.com:8443/group/project/-/issues/1", GL_PORT),
    (f"https://{HOST_TOKEN}@gitlab.example.com/agents/global/-/blob/main/x.md", GL),
    ("http://gitlab.example.com/agents/global/-/issues/1", "http://gitlab.example.com/agents/global"),
    # GitLab: the reference routes (#523) and the id-less list pages (#391)
    (f"{GL}/-/blob/main/README.md", GL),
    (f"{GL}/-/tree/main", GL),
    (f"{GL}/-/raw/main/README.md", GL),
    (f"{GL}/-/wikis/home", GL),
    (f"{GL}/-/commits/main", GL),
    (f"{GL}/-/issues", GL),
    (f"{GL}/-/issues/", GL),
    (f"{GL}/-/issues/?label_name=bug", GL),
    (f"{GL}/-/issues/new", GL),
    (f"{GL}/-/work_items", GL),
    (f"{GL}/-/merge_requests", GL),
    (f"{GL_NESTED}/-/merge_requests", GL_NESTED),
    # GitHub-shaped: owner/repo, whatever follows
    (f"{GH}/pull/9", GH),
    (f"{GH}/pull/9/files", GH),
    (f"{GH}/pulls/9", GH),
    (f"{GH}/issues/456", GH),
    (f"{GH}/issues/456#issuecomment-1", GH),
    (f"{GH}/commit/{FULL_SHA}", GH),
    (f"{GH}/compare/main...feature", GH),
    (f"{GH}/blob/main/README.md", GH),
    (f"{GH}/tree/main", GH),
    (f"{GH}/tree/main/src/pkg", GH),
    (f"{GH}/wiki/Home", GH),
    (f"{GH}/commits/main", GH),
    (f"{GH}/issues", GH),
    (f"{GH}/pulls", GH),
    ("https://github.com/owner/repo.git/pull/9", GH),  # today's derivation drops it
    ("https://ghp-not-a-real-token@github.com/owner/repo/pull/9", GH),
    ("https://github.example.com:8443/owner/repo/pull/9", "https://github.example.com:8443/owner/repo"),
    # a repository: itself, as given — a credential in it is the consumer's
    (GL, GL),
    (f"{GL}.git", f"{GL}.git"),
    (f"{GL}/", f"{GL}/"),
    ("https://github.com/a/b", "https://github.com/a/b"),
    ("https://github.com/a/b/c.git", "https://github.com/a/b/c.git"),
    ("git@gitlab.example.com:agents/global.git", "git@gitlab.example.com:agents/global.git"),
    ("git@github.com:owner/repo", "git@github.com:owner/repo"),
    ("ssh://git@gitlab.example.com:2222/agents/global.git", "ssh://git@gitlab.example.com:2222/agents/global.git"),
    ("git://git.example.com/agents/global.git", "git://git.example.com/agents/global.git"),
    ("file:///srv/git/global.git", "file:///srv/git/global.git"),
    (
        f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/agents/global.git",
        f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/agents/global.git",
    ),
    (f"  {GL}  ", GL),  # as given, whitespace aside
    (".", "."),
    ("./x", "./x"),
    ("/abs/path", "/abs/path"),
    # None: nothing, a thread, other, a non-URL work item
    (None, None),
    ("", None),
    ("   ", None),
    (SLACK, None),
    (MATRIX, None),
    ("https://docs.python.org/3/library/os.html", None),
    ("https://docs.example.com/guide/tree/main", None),
    (f"{GL}?ref=main", None),
    (f"{GL}/-/pipelines/1691", None),
    (f"{GH}/discussions/5", None),
    ("ftp://gitlab.example.com/group/project", None),
    ("ssh://deploy:not-a-real-secret@gitlab.example.com/agents/global.git", None),
    ("deploy@gitlab.example.com:agents/global.git", None),
    ("https://gitlab.example.com/-/issues/5", None),  # a route with no project before it
    (FULL_SHA, None),
    ("abc1234", None),
    ("feature/x", None),
    ("mr-7", None),
    ("pr-9", None),
    ("issue-3", None),
    ("make the flaky test stop", None),
    ("v1.2.3", None),
]


def _repository_id(row):
    url, _ = row
    return repr(url)


#: Both copies of the clone-URL derivation, which now read ``repository_of``.
DERIVES = [
    pytest.param(_derive_repo_url_from_task_target, id="cli"),
    pytest.param(clone_and_exec._derive_repo_url_from_task_target, id="clone_and_exec"),
]


@pytest.fixture
def no_host_tokens(monkeypatch):
    """No token for any host, so the derivation answers in its SSH shape."""
    for name in list(clone_and_exec.os.environ):
        if name.startswith(("GITLAB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The §2 table, against both copies
# ---------------------------------------------------------------------------


class TestTheTable:
    @pytest.mark.parametrize("mod", COPIES)
    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_each_row_classifies_as_the_spec_says(self, mod, row):
        target, klass = row
        assert mod.classify_target(target) == klass

    @pytest.mark.parametrize("mod", COPIES)
    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_is_work_item_is_work_item_or_special(self, mod, row):
        target, klass = row
        assert mod.is_work_item(target) is (klass in ("work-item", "special"))

    def test_the_table_covers_every_class(self):
        assert {klass for _, klass in TABLE} == set(CLASSES)

    @pytest.mark.parametrize("mod", COPIES)
    def test_the_answer_is_always_one_of_the_six(self, mod):
        for target, _ in TABLE:
            assert mod.classify_target(target) in CLASSES

    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_whatever_derives_a_repository_today_is_never_other(self, row, monkeypatch):
        """The constraint the indicator decisions were made under: a URL today's
        ``_derive_repo_url_from_task_target`` reads a repository out of is a
        work item or a reference — never ``other`` (nor a bare repository)."""
        for name in list(clone_and_exec.os.environ):
            if name.startswith("GITLAB_TOKEN"):
                monkeypatch.delenv(name, raising=False)
        target, klass = row
        if clone_and_exec._derive_repo_url_from_task_target(target or "") is not None:
            assert klass in ("work-item", "reference")

    def test_the_indicator_constant_is_segment_and_id(self):
        """The shape decided for the shared constant: ``(segment, id)`` pairs,
        bare segments (both forges' spellings read through one entry), an id
        always required — the id-less list pages live in the reference set."""
        segments = [segment for segment, _ in utils.WORK_ITEM_INDICATORS]
        assert segments == [
            "merge_requests", "issues", "work_items", "pull", "pulls", "commit", "compare",
        ]
        for segment, id_re in utils.WORK_ITEM_INDICATORS:
            assert "/" not in segment and not segment.startswith("-")
            assert id_re  # an id is always required
        assert "-/commits" in utils.REFERENCE_INDICATORS
        assert "commits" in utils.REFERENCE_INDICATORS


# ---------------------------------------------------------------------------
# repository_of, and the derivation that reads it
# ---------------------------------------------------------------------------


class TestRepositoryOf:
    @pytest.mark.parametrize("mod", COPIES)
    @pytest.mark.parametrize("row", REPOSITORY_OF, ids=_repository_id)
    def test_each_row(self, mod, row):
        url, repository = row
        assert mod.repository_of(url) == repository

    @pytest.mark.parametrize("mod", COPIES)
    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_an_answer_exactly_for_a_repository_or_a_url_work_item_or_reference(
        self, mod, row
    ):
        """Over the §2 table: a repository is itself; a work item or a
        reference names its repository when it is a URL (a SHA, a branch,
        ``mr-7`` or prose names none); ``none``, ``special`` and ``other``
        never answer."""
        target, klass = row
        answer = mod.repository_of(target)
        if klass == "repository":
            assert answer == target.strip()
        elif klass in ("work-item", "reference"):
            assert (answer is not None) is ("://" in target)
        else:
            assert answer is None

    @pytest.mark.parametrize("mod", COPIES)
    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_a_derived_answer_is_a_bare_project_url(self, mod, row):
        """What ``_parse_repo_url`` reads (#391): no ``/-/`` route, no tail
        past ``owner/repo``, no query, no fragment — and no credential, which
        this function never adds and, for a derived answer, never carries."""
        target, klass = row
        if klass not in ("work-item", "reference"):
            return
        answer = mod.repository_of(target)
        if answer is None:
            return
        parsed = urlparse(answer)
        assert parsed.scheme in ("http", "https") and parsed.hostname
        assert parsed.username is None and parsed.password is None
        assert not parsed.query and not parsed.fragment
        assert "/-/" not in answer
        assert parsed.path.strip("/"), "a project, never a bare host"

    def test_never_adds_a_credential_even_with_a_host_token(self, monkeypatch):
        """Pure and environment-blind: a token for the host changes nothing."""
        monkeypatch.setenv("GITLAB_TOKEN_gitlab_example_com", HOST_TOKEN)
        monkeypatch.setenv("GITLAB_TOKEN", HOST_TOKEN)
        monkeypatch.setenv("GH_TOKEN", "ghp-not-a-real-token")
        for mod in (utils, clone_and_exec):
            assert mod.repository_of(f"{GL}/-/issues/141") == GL
            assert mod.repository_of(f"{GH}/pull/9") == GH
            assert HOST_TOKEN not in (mod.repository_of(f"{GL}/-/blob/main/x.md") or "")


class TestTheDerivationReadsRepositoryOf:
    @pytest.mark.parametrize("derive", DERIVES)
    @pytest.mark.parametrize("row", TABLE, ids=_row_id)
    def test_derives_exactly_when_a_work_item_or_reference_names_a_repository(
        self, derive, row, no_host_tokens
    ):
        """``_derive_repo_url_from_task_target`` answers for a work item AND a
        reference (#523) and for nothing else: a repository URL derives
        ``None`` so ``spawn._plain_repo_url_from_target``,
        ``clone_secondary_mr`` and ``_resolve_clone_url`` keep falling back
        to the target itself as the clone URL."""
        target, klass = row
        derived = derive(target or "")
        repository = utils.repository_of(target)
        if klass in ("work-item", "reference") and repository is not None:
            assert derived is not None
            assert _parse_repo_url(derived) == _parse_repo_url(repository)
        else:
            assert derived is None

    @pytest.mark.parametrize("derive", DERIVES)
    @pytest.mark.parametrize("row", REPOSITORY_OF, ids=_repository_id)
    def test_the_derived_clone_url_names_the_same_repository(
        self, derive, row, no_host_tokens
    ):
        url, repository = row
        derived = derive(url or "")
        if repository is None or utils.classify_target(url) == "repository":
            assert derived is None
        else:
            assert derived == f"git@{urlparse(repository).hostname}:{urlparse(repository).path.strip('/')}"


# ---------------------------------------------------------------------------
# The special row is the handlers' claim
# ---------------------------------------------------------------------------


HANDLER_FIXTURE_FILES = (
    TESTS / "test_slack_permalink.py",
    TESTS / "test_matrix_chat.py",
)


def _string_literals(path):
    """Every string literal in *path* — the handlers' own fixtures, positive and
    negative, without a hand-copied list that could lag behind them."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


HANDLER_LITERALS = sorted(set().union(*(_string_literals(p) for p in HANDLER_FIXTURE_FILES)))


def _handler_claims(target):
    return SlackThreadTargets.matches(target) or MatrixThreadTargets.matches(target)


class TestHandlerParity:
    def test_the_fixtures_are_really_there(self):
        """Non-vacuous: the canonical Slack permalink, both Matrix shapes and a
        negative or two are among the literals the parity test walks."""
        assert (
            "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456"
            in HANDLER_LITERALS
        )
        assert "https://matrix.to/#/!abc:example.org/$evt?via=example.org" in HANDLER_LITERALS
        assert "https://app.element.io/#/room/!abc:example.org/$evt" in HANDLER_LITERALS
        assert "https://matrix.to/#/!abc:example.org" in HANDLER_LITERALS
        assert "https://ws.slack.com/archives/C0123ABCDEF" in HANDLER_LITERALS
        claimed = [s for s in HANDLER_LITERALS if _handler_claims(s)]
        assert len(claimed) >= 7, claimed

    @pytest.mark.parametrize("mod", COPIES)
    def test_matches_iff_special(self, mod):
        disagreements = [
            (target, _handler_claims(target), mod.classify_target(target))
            for target in [*HANDLER_LITERALS, None]
            if _handler_claims(target) is not (mod.classify_target(target) == "special")
        ]
        assert disagreements == []


# ---------------------------------------------------------------------------
# The repository row is spawn's rule (until spawn calls the classifier)
# ---------------------------------------------------------------------------


SPAWN_FIXTURES = [
    *spawn_fixtures.DERIVABLE_TARGETS,
    *spawn_fixtures.REFERENCE_TARGETS,
    *spawn_fixtures.PLAIN_REPO_TARGETS,
    *spawn_fixtures.NON_REPO_URL_TARGETS,
    *spawn_fixtures.UNDERIVABLE_TARGETS,
    # Embedded credentials, by transport: HTTP(S) is scrubbed and accepted,
    # anything else is refused — on both sides.
    "https://oauth2:glpat-not-a-real-token-000@gitlab.example.com/agents/global.git",
    "ssh://deploy:not-a-real-secret@gitlab.example.com/agents/global.git",
]

#: §2's three deltas from spawn's rules, each a repository to the classifier.
#: ``git://`` is one only on paper (spawn's scheme set already carries it),
#: so what is pinned is whether spawn agrees today.
SPAWN_DELTAS = [
    pytest.param("https://github.com/a/b/c.git", False, id="dot-git-before-the-github-rule"),
    pytest.param("file:///srv/git/global.git", False, id="file-url"),
    pytest.param("git://git.example.com/agents/global.git", True, id="git-url"),
]


class TestSpawnParity:
    @pytest.mark.parametrize("target", SPAWN_FIXTURES)
    def test_repository_iff_spawn_accepts_a_plain_repo_url(self, target, monkeypatch):
        for name in list(spawn.os.environ):
            if name.startswith(("GITLAB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
                monkeypatch.delenv(name, raising=False)
        expected = spawn._plain_repo_url_from_target(target) is not None
        assert (utils.classify_target(target) == "repository") is expected

    @pytest.mark.parametrize("target, spawn_accepts", SPAWN_DELTAS)
    def test_the_stated_deltas(self, target, spawn_accepts):
        assert (spawn._plain_repo_url_from_target(target) is not None) is spawn_accepts
        assert utils.classify_target(target) == "repository"


# ---------------------------------------------------------------------------
# The two copies are one definition
# ---------------------------------------------------------------------------


#: Everything the mirror carries, by name. A name added to one side without
#: the other fails here (``getattr`` on the module that lacks it).
MIRRORED_CONSTANTS = (
    "WORK_ITEM_INDICATORS",
    "REFERENCE_INDICATORS",
    "_WORK_ITEM_RE",
    "_REFERENCE_RE",
    "_TARGET_URL_RE",
    "_SCP_TARGET_RE",
    "_KNOWN_HTTP_FORGES",
    "_FORGE_FIRST_LABELS",
    "_SLACK_MIN_TS_DIGITS",
    "_MATRIX_ROOM_RE",
    "_MATRIX_EVENT_RE",
)
MIRRORED_FUNCTIONS = (
    "_slack_thread_target",
    "_matrix_thread_target",
    "_known_http_forge",
    "_github_http_forge",
    "_repository_url_target",
    "classify_target",
    "is_work_item",
    "repository_of",
)

#: Names a pure function never touches.
IMPURE_NAMES = {"os", "environ", "Path", "open", "subprocess", "sys", "getenv"}


@pytest.fixture(scope="module")
def trees():
    """Both modules' ASTs, for the constant comparison."""
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in (UTILS_PATH, ENTRYPOINT_PATH)
    }


def _assignment(tree, name):
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return ast.unparse(node.value)
    raise AssertionError(f"{name} is not a module-level assignment")


class TestTheTwoCopiesAreOne:
    @pytest.mark.parametrize("name", MIRRORED_CONSTANTS)
    def test_constants_are_spelled_identically(self, trees, name):
        assert _assignment(trees[UTILS_PATH], name) == _assignment(trees[ENTRYPOINT_PATH], name)

    @pytest.mark.parametrize("name", MIRRORED_FUNCTIONS)
    def test_function_bodies_are_identical(self, name):
        canonical, copy = getattr(utils, name), getattr(clone_and_exec, name)
        assert ast_body_lines(canonical) == ast_body_lines(copy)
        assert list(inspect.signature(canonical).parameters) == list(
            inspect.signature(copy).parameters
        )

    def test_the_copy_imports_nothing_from_the_packages(self, trees):
        packages = {"work_repo", "lmer_cli", "lmer_platform", "slack_chat", "matrix_chat"}
        bad = []
        for node in trees[ENTRYPOINT_PATH].body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in packages:
                bad.append(node.lineno)
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] in packages for alias in node.names
            ):
                bad.append(node.lineno)
        assert bad == []

    @pytest.mark.parametrize("name", MIRRORED_FUNCTIONS)
    def test_the_classifier_is_pure(self, name):
        """No filesystem, no environment (§2): nothing in the mirrored
        functions names a module or builtin that would reach either."""
        tree = ast.parse(inspect.getsource(getattr(utils, name)).lstrip())
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert not (names & IMPURE_NAMES), names & IMPURE_NAMES
