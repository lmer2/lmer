"""``lmer_schema.checks.symlinks`` — the one symlink rule two check modules
apply (SCHEMA.md "Inventory": the ``_specs/``/``_plans/`` index links and the
``<session-dir>/<artifact>.md -> masterplan/<artifact>.md`` links).

Three questions in one order, at most one answer per link: is the target
relative, does it stay inside the plexus, does it resolve. This module answers
them and nothing else — it registers no code, emits no finding and never
touches a :class:`~lmer_schema.checks.findings.Report`, which is what makes it
a SUPPORT module both ``index_links`` and ``record`` may import without a
check module importing a sibling (the same standing this package gives
:mod:`lmer_schema.checks.walk`).

Pinned here:

- the three faults and their ORDER — an escaping target is never also
  reported dangling, because "it points outside the tree" is the whole
  answer and resolution is meaningless past it;
- what is NOT a fault: a well-formed relative link (parent hops included, as
  long as they stay inside the root), a link to a directory, and anything
  that is not a symlink at all — a regular ``spec.md`` at a linked name is
  free-form content, never this rule's business;
- that the two callers consume this module rather than carrying a second
  copy of the rules (!256 discussion 5 one level down: two readings of one
  rule drift, and the copy that drifts is the one nobody tests).
"""

from pathlib import Path

import pytest

from lmer_schema.checks import symlinks
from lmer_schema.checks.symlinks import LinkFault, link_fault


def _link(root, name, target):
    """A symlink at *root*/*name* pointing at *target*, verbatim."""
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def _kind(root, link):
    fault = link_fault(root, link)
    return None if fault is None else fault.kind


# ---------------------------------------------------------------------------
# The three faults
# ---------------------------------------------------------------------------


class TestFaults:
    def test_an_absolute_target_is_not_relative(self, tmp_path):
        (tmp_path / "spec.md").write_text("# spec\n", encoding="utf-8")
        link = _link(tmp_path, "session/spec.md", tmp_path / "spec.md")
        fault = link_fault(tmp_path, link)
        assert fault == LinkFault(symlinks.NOT_RELATIVE, str(tmp_path / "spec.md"))

    def test_an_escaping_target_escapes(self, tmp_path):
        root = tmp_path / "plexus"
        root.mkdir()
        (tmp_path / "outside.md").write_text("# outside\n", encoding="utf-8")
        link = _link(root, "session/spec.md", "../../outside.md")
        assert _kind(root, link) == symlinks.ESCAPES

    def test_an_escape_through_a_symlinked_directory_is_caught(self, tmp_path):
        # Lexically the target stays inside; `realpath` is what sees the hop.
        root = tmp_path / "plexus"
        (root / "session").mkdir(parents=True)
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "spec.md").write_text("# spec\n", encoding="utf-8")
        (root / "away").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        link = _link(root, "session/spec.md", "../away/spec.md")
        assert _kind(root, link) == symlinks.ESCAPES

    def test_a_target_that_does_not_exist_dangles(self, tmp_path):
        link = _link(tmp_path, "session/spec.md", "masterplan/spec.md")
        assert link_fault(tmp_path, link) == LinkFault(
            symlinks.DANGLING, "masterplan/spec.md"
        )

    def test_the_target_is_carried_exactly_as_written(self, tmp_path):
        link = _link(tmp_path, "session/plan.md", "./masterplan/../masterplan/plan.md")
        assert link_fault(tmp_path, link).target == "./masterplan/../masterplan/plan.md"


class TestOneAnswerPerLink:
    """The order is the contract: relative, inside, resolves."""

    def test_an_escaping_target_is_not_also_dangling(self, tmp_path):
        root = tmp_path / "plexus"
        root.mkdir()
        link = _link(root, "session/retro.md", "../../nowhere/retro.md")
        assert _kind(root, link) == symlinks.ESCAPES

    def test_an_absolute_target_that_is_missing_is_still_the_shape_fault(self, tmp_path):
        link = _link(tmp_path, "session/plan.md", "/nowhere/plan.md")
        assert _kind(tmp_path, link) == symlinks.NOT_RELATIVE


# ---------------------------------------------------------------------------
# What is not a fault
# ---------------------------------------------------------------------------


class TestWellFormed:
    def test_a_relative_link_that_resolves_says_nothing(self, tmp_path):
        bundle = tmp_path / "session" / "masterplan"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("# spec\n", encoding="utf-8")
        link = _link(tmp_path, "session/spec.md", "masterplan/spec.md")
        assert link_fault(tmp_path, link) is None

    def test_parent_hops_inside_the_root_are_fine(self, tmp_path):
        (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
        link = _link(tmp_path, "project/_specs/2026-08-24-x--readme.md", "../../README.md")
        assert link_fault(tmp_path, link) is None

    def test_a_link_to_a_directory_is_a_link_like_any_other(self, tmp_path):
        (tmp_path / "bundle").mkdir()
        link = _link(tmp_path, "session/masterplan", "../bundle")
        assert link_fault(tmp_path, link) is None

    @pytest.mark.parametrize("kind", ["file", "dir", "missing"])
    def test_what_is_not_a_symlink_is_not_this_rules_business(self, tmp_path, kind):
        path = tmp_path / "spec.md"
        if kind == "file":
            path.write_text("# a real spec, free-form content\n", encoding="utf-8")
        elif kind == "dir":
            path.mkdir()
        assert link_fault(tmp_path, path) is None

    def test_the_root_itself_is_never_an_escape(self, tmp_path):
        (tmp_path / "spec.md").write_text("# spec\n", encoding="utf-8")
        link = _link(tmp_path, "session/spec.md", "../spec.md")
        assert link_fault(tmp_path, link) is None


# ---------------------------------------------------------------------------
# Module surface, and the one rule's two callers
# ---------------------------------------------------------------------------


class TestSupportModule:
    def test_module_exports_its_surface(self):
        for name in symlinks.__all__:
            assert hasattr(symlinks, name), name
        assert symlinks.KINDS == (
            symlinks.NOT_RELATIVE,
            symlinks.ESCAPES,
            symlinks.DANGLING,
        )

    def test_it_registers_no_code_and_emits_no_finding(self):
        # The support-module invariant (tests/test_lmer_schema_runner.py keeps
        # the same guard over the package): a module that grew a finding is a
        # check module nobody wired in.
        source = Path(symlinks.__file__).read_text(encoding="utf-8")
        assert "register(" not in source
        assert "report.add(" not in source
        assert "from lmer_schema.checks.findings import" not in source

    @pytest.mark.parametrize("module", ["index_links", "record"])
    def test_both_callers_use_this_rule_instead_of_a_copy(self, module):
        import importlib

        source = Path(
            importlib.import_module(f"lmer_schema.checks.{module}").__file__
        ).read_text(encoding="utf-8")
        assert "link_fault(" in source, module
        for spelling in ("os.path.isabs", "os.path.realpath", "os.readlink"):
            assert spelling not in source, f"{module} re-implements {spelling}"
