"""The guard behind the gate's text-only fast path (issue #269).

`gate-check` runs the tests declared in `.lmer/gate-check.yaml` under
`tests.text_diff_subset` — instead of the whole suite — when a change touches
nothing but prose. That is only sound while the declaration lists EVERY test
that reads a path the gate calls text; the first unlisted doc-reading test
turns the fast path into a silent waiver.

So the declaration is not maintained by hand. This module scans `tests/` for
repo-rooted path expressions, classifies them with the gate's own
`is_text_diff_path`, and fails with the exact list a stale declaration is
missing.

What the scan MODELS:

- Repo-anchored `pathlib` composition: `Path(__file__).parent.parent`, names
  assigned from such an expression, `/` composition and `joinpath` below
  them, and the conftest fixtures that yield repo paths (`project_root`,
  `rules_dir`, ...). Within that vocabulary it fails closed — a segment it
  cannot resolve to a literal (`project_root / 'bin' / script_name`) is
  unclassifiable, and unclassifiable means demanded.
- `glob`/`rglob` on a repo-rooted base whose pattern could match a text file,
  including a pattern built at runtime (`test_security.py`'s extension loop).
  Those are what empties a naive allowlist.
- Attribution, not resolution, for reads reached through a shared helper: the
  conftest fixtures are attributed to the tests that request them, and any
  other helper module that reads text fails the scan with instructions rather
  than being followed.

What it does NOT model — every one of these reads repo text INVISIBLY to it,
and none is demanded:

- cwd-relative `open('README.md')` (the gate runs from the repo root);
- `os.path.join(REPO, 'README.md')` and string/f-string path building;
- `os.walk`, `glob.glob`, `iterdir`;
- a test that shells out to a repo script which itself reads a doc.

So the declaration is a FLOOR, not a proof: a scan that finds nothing new
does not mean the subset is complete, and a project may list more than the
scan demands. What holds in this repo today is a MEASUREMENT, not a
mechanism — the undeclared modules that reach a real repo path through an
unmodelled idiom reach paths the gate does not call text, and every other
such idiom in `tests/` addresses `tmp_path` (audited by hand; see
`.lmer/gate-check.yaml`). A test written with one of those idioms against a
repo doc has to be added to `tests.text_diff_subset` by hand; nothing here
will ask for it.
"""
import ast
import re
from pathlib import Path

import pytest
import yaml

from lmer_cli.gates import is_text_diff_path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
GATE_CONFIG = REPO_ROOT / ".lmer" / "gate-check.yaml"

# A path segment the scan could not resolve to a literal. Any path carrying
# one is unclassifiable, and unclassifiable means "demand inclusion".
UNRESOLVED = "<?>"

# Names substituted for a glob's wildcards to ask "could this pattern match
# something the gate calls text?". `*` crossing `/` is why one of them is a
# nested path: `rglob('*')` reaches a doc several directories down.
WILDCARD_WITNESSES = (
    "witness", "witness.md", "witness.rst", "witness.txt",
    "LICENSE", "CHANGELOG.yaml", "20260101-topic.yaml", "sub/witness.md",
)


class RepoPath:
    """A path anchored at the repo root, as the parts below it.

    `parts is None` means "somewhere in the repo, location unknown" — an
    anchor the scan could not follow, e.g. `Path.cwd()`.
    """

    def __init__(self, parts):
        self.parts = parts

    def relative(self):
        """The repo-relative path, or None when any segment is unresolved."""
        if self.parts is None or UNRESOLVED in self.parts:
            return None
        return "/".join(self.parts)

    def joined(self, segment):
        if self.parts is None:
            return RepoPath(None)
        if segment is None:
            return RepoPath(self.parts + (UNRESOLVED,))
        pieces = [p for p in segment.split("/") if p not in ("", ".")]
        return RepoPath(self.parts + tuple(pieces))

    def parent(self):
        # Above the repo root the scan is out of its depth; say so rather
        # than pretending the path is elsewhere.
        if not self.parts:
            return RepoPath(None)
        return RepoPath(self.parts[:-1])


class _Touch:
    """One place a module reaches for a repo path, with the reason it counts."""

    def __init__(self, lineno, reason):
        self.lineno = lineno
        self.reason = reason

    def __str__(self):
        return f"line {self.lineno}: {self.reason}"


class ModuleScan:
    """Finds the repo text paths one test module reaches for."""

    def __init__(self, path, anchor_fixtures=None, text_fixtures=frozenset(),
                 tree=None, names=None):
        self.path = path
        self.anchor_fixtures = dict(anchor_fixtures or {})
        self.text_fixtures = frozenset(text_fixtures)
        self.tree = tree if tree is not None else ast.parse(
            path.read_text(encoding="utf-8"))
        self.names = dict(names or {})
        self.touches = []

    # -- resolving repo-rooted expressions --------------------------------
    def resolve(self, node):
        """The RepoPath `node` evaluates to, or None if it is not one."""
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            if node.id in self.anchor_fixtures:
                return RepoPath(self.anchor_fixtures[node.id])
            return None

        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            if node.attr == "parent" and base is not None:
                return base.parent()
            if node.attr == "parents" and base is not None:
                return RepoPath(None)
            # A class attribute or `self.X` bound to a repo path elsewhere in
            # the module; names are tracked module-wide, attribute included.
            if base is None and node.attr in self.names:
                return self.names[node.attr]
            return None

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.resolve(node.left)
            if base is None:
                return None
            return base.joined(_literal(node.right))

        if isinstance(node, ast.Call):
            return self._resolve_call(node)

        return None

    def _resolve_call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Path" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "__file__":
                return RepoPath(self.path.relative_to(REPO_ROOT).parts)
            return self.resolve(first)

        if not isinstance(func, ast.Attribute):
            return None

        if func.attr == "cwd":
            # The gate runs from the repo root, so tests using it are reaching
            # into the repo at an address the scan cannot pin down.
            return RepoPath(None)

        base = self.resolve(func.value)
        if base is None:
            return None
        if func.attr in ("resolve", "absolute", "expanduser"):
            return base
        if func.attr == "joinpath":
            for arg in node.args:
                base = base.joined(_literal(arg))
            return base
        return None

    def bind_names(self):
        """Bind every name assigned a repo-rooted path, to a fixed point.

        Flow-insensitive and module-wide on purpose: a name reused for a
        different value elsewhere only ever makes the scan demand MORE.
        """
        assignments = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                assignments.append((node.targets, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                assignments.append(([node.target], node.value))

        for _ in range(8):
            before = len(self.names)
            for targets, value in assignments:
                resolved = self.resolve(value)
                if resolved is None:
                    continue
                for target in targets:
                    name = getattr(target, "id", None) or getattr(
                        target, "attr", None)
                    if name and name not in self.names:
                        self.names[name] = resolved
            if len(self.names) == before:
                break

    # -- classifying what it finds ----------------------------------------
    def scan(self):
        self.bind_names()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("glob", "rglob"):
                base = self.resolve(node.func.value)
                if base is not None:
                    pattern = _literal(node.args[0]) if node.args else None
                    self._judge_glob(node, base, pattern)
                continue
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                resolved = self.resolve(node)
                if resolved is not None:
                    self._judge_path(node.lineno, resolved)
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._judge_fixture_params(node)
        return self.touches

    def _judge_path(self, lineno, repo_path):
        relative = repo_path.relative()
        if relative is None:
            self.touches.append(_Touch(
                lineno, f"unclassifiable repo path {_show(repo_path)}"))
        elif is_text_diff_path(relative):
            self.touches.append(_Touch(lineno, f"reads {relative}"))

    def _judge_glob(self, node, base, pattern):
        where = _show(base)
        if pattern is None:
            self.touches.append(_Touch(
                node.lineno,
                f"{node.func.attr}() with a runtime pattern under {where}"))
            return
        prefix = base.relative()
        if prefix is None:
            self.touches.append(_Touch(
                node.lineno, f"{node.func.attr}({pattern!r}) under {where}"))
            return
        match = _glob_could_match_text(prefix, pattern,
                                       recursive=node.func.attr == "rglob")
        if match is not None:
            self.touches.append(_Touch(
                node.lineno,
                f"{node.func.attr}({pattern!r}) under {prefix or '.'} "
                f"can match {match}"))

    def _judge_fixture_params(self, node):
        params = [a.arg for a in node.args.args + node.args.kwonlyargs]
        for param in sorted(set(params) & self.text_fixtures):
            self.touches.append(_Touch(
                node.lineno, f"uses the text-reading fixture {param!r}"))


def _literal(node):
    """The string a node evaluates to, or None when it is not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _show(repo_path):
    if repo_path.parts is None:
        return "<unknown location>"
    return "/".join(repo_path.parts) or "."


def _glob_could_match_text(prefix, pattern, recursive):
    """A text path the glob could match from `prefix`, or None.

    Asked by construction rather than by matching: fill the pattern's
    wildcards with witness names and put the result through the gate's own
    classifier. A pattern with no wildcard left to fill is its own witness.
    """
    bases = [prefix]
    if recursive:
        # rglob descends, so the pattern can also land below its base.
        bases.append(f"{prefix}/sub".lstrip("/"))
    for base in bases:
        for witness in WILDCARD_WITNESSES:
            filled = re.sub(r"\*\*|\*|\?", witness, pattern)
            candidate = f"{base}/{filled}" if base else filled
            if is_text_diff_path(candidate):
                return candidate
    return None


def conftest_fixture_roles():
    """conftest fixtures that yield repo paths, and those that read text.

    Derived rather than listed: a new `docs_dir` fixture must anchor the
    scan the day it is written, not the day someone remembers this file.
    """
    path = TESTS_DIR / "conftest.py"
    module = ModuleScan(path)
    module.bind_names()
    fixtures = [
        node for node in ast.walk(module.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_fixture_decorator(d) for d in node.decorator_list)
    ]

    anchors, text_readers = {}, set()
    # To a fixed point, so a fixture that requests one defined further down
    # the file is classified the same as one that requests an earlier one.
    for _ in range(4):
        before = (len(anchors), len(text_readers))
        for node in fixtures:
            body = ModuleScan(path, anchor_fixtures=anchors,
                              text_fixtures=text_readers, tree=node,
                              names=module.names)
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Return, ast.Yield)) and inner.value is not None:
                    resolved = body.resolve(inner.value)
                    if resolved is not None:
                        anchors[node.name] = resolved.parts
            if body.scan():
                text_readers.add(node.name)
        if (len(anchors), len(text_readers)) == before:
            break
    return anchors, frozenset(text_readers)


def _is_fixture_decorator(decorator):
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(node, ast.Attribute) and node.attr == "fixture"


def conftest_touches_outside_fixtures():
    """conftest text reads that no fixture owns — nobody could attribute them.

    A read from module scope or a plain helper belongs to whichever tests
    reach it, which this scan cannot tell; the attribution it does do is
    fixture-by-fixture.
    """
    anchors, text_fixtures = conftest_fixture_roles()
    path = TESTS_DIR / "conftest.py"
    module = ModuleScan(path, anchors, text_fixtures)
    touches = module.scan()
    fixture_spans = [
        (node.lineno, node.end_lineno)
        for node in ast.walk(module.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_fixture_decorator(d) for d in node.decorator_list)
    ]
    return [touch for touch in touches
            if not any(start <= touch.lineno <= end
                       for start, end in fixture_spans)]


def scan_test_tree():
    """Repo-relative test path → the text touches that demand its inclusion."""
    anchors, text_fixtures = conftest_fixture_roles()
    demanded = {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == "conftest.py":
            # Not a runnable subset entry: its fixtures are attributed to the
            # tests that request them.
            continue
        touches = ModuleScan(path, anchors, text_fixtures).scan()
        if touches:
            demanded[path.relative_to(REPO_ROOT).as_posix()] = touches
    return demanded


def declared_subset():
    """`tests.text_diff_subset` from the repo's own gate-check config."""
    config = yaml.safe_load(GATE_CONFIG.read_text(encoding="utf-8")) or {}
    return (config.get("tests") or {}).get("text_diff_subset") or []


@pytest.fixture(scope="module")
def demanded():
    return scan_test_tree()


class TestDeclarationCoversEveryTextReader:
    """The declaration is what makes the fast path sound; keep it complete."""

    def test_every_text_reading_test_file_is_declared(self, demanded):
        declared = set(declared_subset())
        modules = {p for p in demanded if Path(p).name.startswith("test_")}
        missing = sorted(modules - declared)
        detail = "\n".join(
            f"  {path}\n" + "\n".join(f"      {t}" for t in demanded[path][:3])
            for path in missing
        )
        assert not missing, (
            "these test files read repo text but are not in "
            f"{GATE_CONFIG.relative_to(REPO_ROOT)} → tests.text_diff_subset, "
            "so a docs-only change would not run them:\n" + detail
        )

    def test_every_declared_path_exists(self):
        declared = declared_subset()
        assert declared, "the subset is declared, or there is no fast path"
        missing = [p for p in declared if not (REPO_ROOT / p).exists()]
        # pytest exits on a usage error for a path that is not there, which
        # the gate would report as a test failure.
        assert not missing, f"declared but absent: {missing}"

    def test_no_shared_helper_reads_repo_text_unattributed(self, demanded):
        """A helper module is not a subset entry — it is a dependency.

        Listing `tests/_helper.py` would run nothing; what has to be declared
        is every test module that imports it. Fail here rather than let the
        read pass unnoticed.
        """
        helpers = sorted(p for p in demanded
                         if not Path(p).name.startswith("test_"))
        assert not helpers, (
            f"{helpers} read repo text but are not test modules: declare the "
            "test files that import them in tests.text_diff_subset"
        )

    def test_the_tests_tree_has_exactly_one_conftest(self):
        """The scan skips every `conftest.py`; it only ATTRIBUTES one.

        `scan_test_tree` drops conftest modules at any depth because they are
        not runnable subset entries, while `conftest_fixture_roles` reads
        `tests/conftest.py` alone. A second conftest further down would have
        its fixtures skipped by the first rule and attributed by neither — a
        doc read owned by nobody. Fail here rather than there.
        """
        conftests = sorted(p.relative_to(REPO_ROOT).as_posix()
                           for p in TESTS_DIR.rglob("conftest.py"))
        assert conftests == ["tests/conftest.py"], (
            f"{conftests}: the scan attributes fixtures from "
            "tests/conftest.py only — teach conftest_fixture_roles about the "
            "others before adding one"
        )

    def test_conftest_text_reads_are_all_owned_by_a_fixture(self):
        """The attribution the scan performs is per fixture; keep it that way."""
        stray = conftest_touches_outside_fixtures()
        assert not stray, (
            "conftest.py reads repo text outside a fixture "
            f"({[str(t) for t in stray]}): every test in the suite may depend "
            "on it, and the scan cannot say which"
        )

    def test_the_known_glob_readers_are_demanded(self, demanded):
        """The measurement that killed "text-only diffs skip the suite".

        These four walk the repo and assert on file CONTENT, so a docs-only
        diff genuinely can fail them — the reason the honest skip list is
        empty and this is a subset instead.
        """
        for path in ("tests/test_security.py", "tests/test_consistency.py",
                     "tests/test_integration.py"):
            assert path in demanded, f"{path} no longer scans as a text reader"


class TestScanner:
    """The scanner's own contract: it must fail closed."""

    def _scan_source(self, tmp_path, source, name="test_probe.py"):
        path = TESTS_DIR / name  # the anchor is relative to the tests dir
        module = ModuleScan(path, {"project_root": ()}, frozenset(),
                            tree=ast.parse(source))
        return module.scan()

    def test_named_doc_read_is_found(self, tmp_path):
        touches = self._scan_source(tmp_path, (
            "from pathlib import Path\n"
            "REPO = Path(__file__).parent.parent\n"
            "def test_x():\n"
            "    (REPO / 'docs' / 'LMER-CLI.md').read_text()\n"
        ))
        assert any("docs/LMER-CLI.md" in t.reason for t in touches)

    def test_non_text_read_is_not_demanded(self, tmp_path):
        touches = self._scan_source(tmp_path, (
            "from pathlib import Path\n"
            "REPO = Path(__file__).parent.parent\n"
            "def test_x():\n"
            "    (REPO / 'src' / 'lmer_cli' / 'cli.py').read_text()\n"
        ))
        assert touches == []

    def test_tmp_path_lookalike_is_not_demanded(self, tmp_path):
        """A test writing its own README.md in tmp_path reads no repo text."""
        touches = self._scan_source(tmp_path, (
            "def test_x(tmp_path):\n"
            "    (tmp_path / 'README.md').write_text('hi')\n"
        ))
        assert touches == []

    def test_literal_glob_over_markdown_is_found(self, tmp_path):
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root):\n"
            "    list(project_root.glob('rules/*.md'))\n"
        ))
        assert any("rules/*.md" in t.reason for t in touches)

    def test_rglob_with_a_runtime_pattern_is_demanded(self, tmp_path):
        """test_security.py's shape: the pattern comes from a variable."""
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root):\n"
            "    for ext in ['.py', '.md']:\n"
            "        list(project_root.rglob(f'*{ext}'))\n"
        ))
        assert any("runtime pattern" in t.reason for t in touches)

    def test_glob_that_cannot_match_text_is_not_demanded(self, tmp_path):
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root):\n"
            "    list((project_root / 'web' / 'src').rglob('*.vue'))\n"
        ))
        assert touches == []

    def test_unresolved_segment_fails_closed(self, tmp_path):
        """`bin/{name}` could be `bin/notes.md`; unknown means demanded."""
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root, script_name):\n"
            "    (project_root / 'bin' / script_name).read_text()\n"
        ))
        assert any("unclassifiable" in t.reason for t in touches)

    def test_prose_under_docs_is_text_at_any_depth(self, tmp_path):
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root):\n"
            "    (project_root / 'docs' / 'sub' / 'guide.md').read_text()\n"
        ))
        assert any("docs/sub/guide.md" in t.reason for t in touches)

    def test_a_non_prose_file_under_docs_is_not_text(self, tmp_path):
        """`docs/` is a location, not a role: what is under it can execute."""
        touches = self._scan_source(tmp_path, (
            "def test_x(project_root):\n"
            "    (project_root / 'docs' / 'conf.py').read_text()\n"
        ))
        assert touches == []


class TestConftestRoles:
    """The fixture-derived anchors the whole scan rests on."""

    def test_project_root_and_rules_dir_anchor_the_repo(self):
        anchors, _ = conftest_fixture_roles()
        assert anchors["project_root"] == ()
        assert anchors["rules_dir"] == ("rules",)

    def test_a_fixture_that_reads_text_is_flagged_as_one(self):
        _, text_fixtures = conftest_fixture_roles()
        # `main_config` hands out AGENTS.md, `all_rule_files` globs rules/*.md:
        # whoever requests them reads text without naming a path themselves.
        assert "main_config" in text_fixtures
        assert "all_rule_files" in text_fixtures
