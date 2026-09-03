"""``lmer_schema.checks.registry`` — the registry check module (spec §4, §7).

The module reports FINDINGS about the ``projects:`` registry of a manifest
that already loaded. That sentence is the whole design, and these tests pin
both halves of it:

- **what it says** — the §4 registry corners that are nudges rather than
  refusals: ``remotes: []`` beside ``repo:`` (pointless, harmless), a
  non-canonical spelling (scheme, userinfo, trailing ``.git`` or ``/``) now
  that ``work adopt`` always writes the canonical form, and a repoless entry
  (D19) with no ``description:`` — legal, but nothing says what the tree is
  for. A repoless entry itself is never a finding, and the port corner is
  explicitly NOT a duplicate: ``git@host:a/b`` and
  ``ssh://git@host:2222/a/b`` in one entry are two remotes, because a
  non-default port makes a different key (§4);
- **what it does not say** — the identity rules are enforced one layer down
  and RAISE: the loader owns the §1 key charset and the ``repo:``/
  ``remotes:`` types, ``build_registry`` owns "``remotes:`` without
  ``repo:``", the primary repeated inside its own ``remotes:``, two values
  in one entry folding to one key, one remote claimed by two entries, and an
  embedded credential. ``TestTheLoadersJob`` pins that those never reach
  this module, and ``TestBrokenRegistry`` pins the one thing it does with
  them: a registry that does not build is ONE ``registry.invalid`` ERROR
  finding (never a re-check, never a crash, never a leaked credential or
  machine path).

Every negative test mutates ITS OWN copy of the fixture manifest document;
the shared builder is consumed read-only.
"""

import ast
import json
from pathlib import Path

import pytest
import yaml

from lmer_schema.checks import CHECKS, Report, Severity, Tier
from lmer_schema.checks.registry import check_registry
from lmer_schema.manifest import (
    MANIFEST_NAME,
    ManifestError,
    load_manifest,
    parse_manifest,
)
from lmer_schema.registry import Registry, load_registry
from lmer_schema.urls import normalize
from tests import plexus_fixtures as pf

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "src" / "lmer_schema" / "checks" / "registry.py"

HOST, PATH = pf.PRIMARY_REPO.split("/", 1)

# Fake token value (glpat- shape, not a real secret).
TOKEN = "glpat-FAKEtoken123"

#: Every code this module owns — the whole `registry.` namespace.
CODES = (
    "registry.invalid",
    "registry.non-canonical-remote",
    "registry.remotes-empty",
    "registry.repoless-without-description",
)

#: Non-canonical spellings of the fixture's primary remote (§4). Each is the
#: SAME repo — the nudge is about the spelling, never about identity.
SPELLINGS = {
    "https": f"https://{pf.PRIMARY_REPO}",
    "git-suffix": f"https://{pf.PRIMARY_REPO}.git",
    "trailing-slash": f"{pf.PRIMARY_REPO}/",
    "scp-form": f"git@{HOST}:{PATH}",
    "ssh-scheme": f"ssh://git@{HOST}/{PATH}",
    "default-port": f"https://{HOST}:443/{PATH}",
}


def _doc():
    """The §4 fixture manifest as a plain dict, for targeted mutation."""
    return yaml.safe_load(pf.SCHEMA_MANIFEST)


def _text(doc):
    return yaml.safe_dump(doc, sort_keys=False)


def _entry(**keys):
    """A document whose ONE project is *keys* (no other entry in the way)."""
    doc = _doc()
    doc["projects"] = {pf.PROJECT: keys}
    return doc


def _check(doc, *, form=pf.SCHEMA_FORM, path=None):
    """Run the module over *doc* and return ``(report, registry)``."""
    report = Report(form)
    registry = check_registry(report, parse_manifest(_text(doc), path=path))
    return report, registry


def _codes(report):
    return [finding.code for finding in report.findings]


def _one(report, code):
    """The single finding of *code*, with the whole report in the failure."""
    matching = [finding for finding in report.findings if finding.code == code]
    assert len(matching) == 1, _codes(report)
    return matching[0]


def _text_of(finding):
    return f"{finding.message} {finding.detail or ''}"


# ---------------------------------------------------------------------------
# The codes
# ---------------------------------------------------------------------------


class TestCodes:
    """The module registers exactly the codes it emits, in its own namespace.

    Registering a code no check can reach would advertise a check in
    ``--list-checks`` that never fires; the identity rules are the loader's
    and raise, so they get no code here (module docstring).
    """

    def test_namespace_holds_exactly_these_codes(self):
        registered = {
            entry.code for entry in CHECKS if entry.namespace == "registry"
        }
        assert registered == set(CODES)

    def test_the_registry_does_not_build_is_the_only_error(self):
        assert CHECKS["registry.invalid"].severity is Severity.ERROR
        for code in CODES:
            if code != "registry.invalid":
                assert CHECKS[code].severity is Severity.WARN, code

    def test_no_code_is_tier_sensitive(self):
        # A legacy tree has no manifest, so it has no registry to check:
        # nothing here can fire under the legacy tier and there is nothing
        # for D14 to downgrade.
        softened = {entry.code for entry in CHECKS.tier_sensitive()}
        assert softened.isdisjoint(CODES)
        for code in CODES:
            assert CHECKS[code].legacy is CHECKS[code].severity, code

    def test_every_code_checks_the_manifest_machine_surface(self):
        for code in CODES:
            assert CHECKS[code].tier is Tier.MACHINE, code


# ---------------------------------------------------------------------------
# The clean cases
# ---------------------------------------------------------------------------


class TestCleanRegistry:
    def test_the_fixture_registry_says_nothing(self, schema_plexus):
        report = Report(schema_plexus.form)
        registry = check_registry(report, load_manifest(schema_plexus.root))

        assert report.findings == ()
        assert report.exit_code == 0
        assert isinstance(registry, Registry)

    def test_it_returns_the_built_registry_for_the_next_check(self, schema_plexus):
        # The tree checks (registry-to-dir, bucket collision) need the same
        # registry; handing it back keeps one build per run.
        registry = check_registry(Report(schema_plexus.form), load_manifest(schema_plexus.root))

        assert sorted(registry) == sorted([pf.PROJECT, pf.REPOLESS_PROJECT])
        assert registry.lookup(f"https://{pf.PRIMARY_REPO}.git").name == pf.PROJECT
        assert registry.lookup(pf.REMOTES[0]).name == pf.PROJECT

    def test_a_legacy_tree_has_no_registry_to_check(self, legacy_plexus):
        # No manifest is the legacy-form signal (D20); the recognized-legacy
        # INFO belongs to the manifest check module, not to this one.
        manifest = load_manifest(legacy_plexus.root)
        report = Report(legacy_plexus.form)

        assert manifest is None
        assert check_registry(report, manifest) is None
        assert report.findings == ()

    def test_an_empty_registry_says_nothing(self):
        doc = _doc()
        doc["projects"] = {}
        report, registry = _check(doc)

        assert report.findings == ()
        assert len(registry) == 0

    def test_every_finding_is_reported_against_the_manifest(self):
        doc = _entry(remotes=[])
        doc["projects"][pf.PROJECT]["repo"] = SPELLINGS["git-suffix"]
        report, _ = _check(doc)

        assert len(report.findings) == 2
        assert {finding.path for finding in report.findings} == {MANIFEST_NAME}


# ---------------------------------------------------------------------------
# Repoless entries (D19)
# ---------------------------------------------------------------------------


class TestRepolessEntries:
    def test_a_described_repoless_entry_is_never_a_finding(self):
        report, registry = _check(
            _entry(description="an ops workstream with no repo"),
        )

        assert report.findings == ()
        assert registry[pf.PROJECT].repoless
        assert registry[pf.PROJECT].keys == ()

    def test_a_repoless_entry_without_a_description_is_a_warn_nudge(self):
        report, _ = _check(_entry())
        finding = _one(report, "registry.repoless-without-description")

        assert finding.severity is Severity.WARN
        assert f"projects.{pf.PROJECT}" in finding.message
        assert "description" in finding.message
        # A nudge never blocks (D14).
        assert report.exit_code == 0

    def test_a_blank_description_is_the_same_nudge(self):
        report, _ = _check(_entry(description="   "))

        assert _codes(report) == ["registry.repoless-without-description"]

    def test_a_repo_backed_entry_needs_no_description(self):
        report, _ = _check(_entry(repo=pf.PRIMARY_REPO))

        assert report.findings == ()


# ---------------------------------------------------------------------------
# `remotes: []` beside `repo:` (§4 registry corners)
# ---------------------------------------------------------------------------


class TestEmptyRemotesList:
    def test_an_empty_list_beside_repo_is_a_warn(self):
        report, _ = _check(_entry(repo=pf.PRIMARY_REPO, remotes=[]))
        finding = _one(report, "registry.remotes-empty")

        assert finding.severity is Severity.WARN
        assert f"projects.{pf.PROJECT}.remotes:" in finding.message
        assert report.exit_code == 0

    def test_an_absent_remotes_key_is_not_an_empty_one(self):
        # A single-remote project is one `repo:` line with no list (§4).
        report, _ = _check(_entry(repo=pf.PRIMARY_REPO))

        assert report.findings == ()

    def test_a_populated_list_is_not_a_finding(self):
        report, _ = _check(_entry(repo=pf.PRIMARY_REPO, remotes=list(pf.REMOTES)))

        assert report.findings == ()


# ---------------------------------------------------------------------------
# Non-canonical spellings (§4 registry corners)
# ---------------------------------------------------------------------------


class TestNonCanonicalSpelling:
    @pytest.mark.parametrize("spelling", sorted(SPELLINGS), ids=sorted(SPELLINGS))
    def test_each_non_canonical_spelling_of_the_primary_warns(self, spelling):
        value = SPELLINGS[spelling]
        report, registry = _check(_entry(repo=value))
        finding = _one(report, "registry.non-canonical-remote")

        assert finding.severity is Severity.WARN
        assert f"projects.{pf.PROJECT}.repo:" in finding.message
        # The nudge is about the spelling only: same repo, same key.
        assert pf.PRIMARY_REPO in _text_of(finding)
        assert registry.lookup(value).name == pf.PROJECT
        assert report.exit_code == 0

    def test_the_canonical_spelling_is_silent(self):
        report, _ = _check(_entry(repo=pf.PRIMARY_REPO, remotes=list(pf.REMOTES)))

        assert report.findings == ()

    def test_a_credential_free_userinfo_is_still_a_spelling_nudge(self):
        report, _ = _check(_entry(repo=f"ssh://git@{HOST}/{PATH}"))

        assert _codes(report) == ["registry.non-canonical-remote"]

    def test_a_remote_is_named_by_its_list_position(self):
        report, _ = _check(
            _entry(repo=pf.PRIMARY_REPO, remotes=[pf.REMOTES[0], f"{pf.REMOTES[1]}.git"]),
        )
        finding = _one(report, "registry.non-canonical-remote")

        assert f"projects.{pf.PROJECT}.remotes:[1]" in finding.message

    def test_each_offending_value_gets_its_own_finding(self):
        report, _ = _check(
            _entry(
                repo=SPELLINGS["git-suffix"],
                remotes=[f"https://{pf.REMOTES[0]}", f"{pf.REMOTES[1]}/"],
            ),
        )

        assert _codes(report) == ["registry.non-canonical-remote"] * 3

    def test_the_nudge_never_calls_the_two_spellings_different_repos(self):
        report, registry = _check(_entry(repo=SPELLINGS["git-suffix"]))

        assert registry[pf.PROJECT].repo == SPELLINGS["git-suffix"]
        assert registry[pf.PROJECT].keys == (pf.PRIMARY_REPO,)
        assert report.exit_code == 0


class TestPortCorner:
    """A non-default port makes a DIFFERENT key (§4), so the two values are
    two remotes rather than one repo spelled twice."""

    PORTED = f"ssh://git@{HOST}:2222/{PATH}"
    CANONICAL_PORTED = f"{HOST}:2222/{PATH}"

    def test_the_same_path_on_another_port_is_a_second_remote(self):
        report, registry = _check(
            _entry(repo=f"git@{HOST}:{PATH}", remotes=[self.PORTED]),
        )

        # Two keys, one project — NOT the duplicate-within-one-entry ERROR.
        assert "registry.invalid" not in _codes(report)
        assert registry[pf.PROJECT].keys == (pf.PRIMARY_REPO, self.CANONICAL_PORTED)
        assert registry.lookup(self.PORTED).name == pf.PROJECT
        assert report.exit_code == 0

    def test_both_values_are_still_nudged_on_spelling(self):
        report, _ = _check(_entry(repo=f"git@{HOST}:{PATH}", remotes=[self.PORTED]))

        assert _codes(report) == ["registry.non-canonical-remote"] * 2

    def test_the_canonical_ported_spelling_is_silent(self):
        # `host:port/path` is what normalization emits, and it is a fixed
        # point — the canonical form of a ported remote never nudges.
        assert normalize(self.CANONICAL_PORTED) == self.CANONICAL_PORTED
        report, _ = _check(
            _entry(repo=pf.PRIMARY_REPO, remotes=[self.CANONICAL_PORTED]),
        )

        assert report.findings == ()


# ---------------------------------------------------------------------------
# A registry that does not build
# ---------------------------------------------------------------------------


class TestBrokenRegistry:
    """The identity rules RAISE one layer down (D8/§4). This module turns
    that raise into ONE ERROR finding so ``lmer-schema validate`` prints a
    fix-list instead of a traceback; a caller that wants the exit-2 unusable
    path calls ``load_registry`` and lets it raise (``TestTheLoadersJob``).
    """

    def _broken(self, doc, *needles):
        report, registry = _check(doc)
        finding = _one(report, "registry.invalid")

        assert finding.severity is Severity.ERROR
        assert report.exit_code == 1
        assert registry is None
        for needle in needles:
            assert needle in _text_of(finding), (needle, _text_of(finding))
        return finding

    def test_one_remote_claimed_by_two_projects(self):
        doc = _doc()
        doc["projects"]["fork"] = {"repo": pf.PRIMARY_REPO}

        self._broken(doc, pf.PROJECT, "fork", pf.PRIMARY_REPO)

    def test_remotes_without_repo(self):
        self._broken(_entry(remotes=list(pf.REMOTES)), "remotes:", "repo:")

    def test_the_primary_repeated_inside_its_own_remotes(self):
        doc = _entry(repo=pf.PRIMARY_REPO, remotes=[f"https://{pf.PRIMARY_REPO}.git"])

        self._broken(doc, "repo:", "remotes:")

    def test_two_values_in_one_entry_folding_to_one_key(self):
        # §4 registry corners: twice within ONE entry is an error, exactly
        # like twice across entries.
        mirror_host, mirror_path = pf.REMOTES[0].split("/", 1)
        doc = _entry(
            repo=pf.PRIMARY_REPO,
            remotes=[pf.REMOTES[0], f"git@{mirror_host}:{mirror_path}.git"],
        )

        self._broken(doc, "twice")

    def test_an_embedded_credential_is_reported_without_echoing_it(self):
        doc = _entry(repo=f"https://oauth2:{TOKEN}@{HOST}/{PATH}.git")
        report, _ = _check(doc)

        assert _codes(report) == ["registry.invalid"]
        assert TOKEN not in json.dumps(report.to_dict())

    def test_the_finding_names_the_manifest_and_leaks_no_machine_path(self, schema_plexus):
        doc = _doc()
        doc["projects"]["fork"] = {"repo": pf.PRIMARY_REPO}
        schema_plexus.manifest.write_text(_text(doc), encoding="utf-8")

        report = Report(schema_plexus.form)
        check_registry(report, load_manifest(schema_plexus.root))
        finding = _one(report, "registry.invalid")

        assert finding.path == MANIFEST_NAME
        assert str(schema_plexus.root) not in _text_of(finding)

    def test_the_soft_nudges_are_suppressed_while_the_registry_is_broken(self):
        # One union, one answer: with an identity rule broken the registry is
        # not trustworthy, and spelling nits ahead of the error are noise.
        doc = _doc()
        doc["projects"][pf.PROJECT]["repo"] = SPELLINGS["git-suffix"]
        doc["projects"]["fork"] = {"repo": pf.PRIMARY_REPO}
        report, _ = _check(doc)

        assert _codes(report) == ["registry.invalid"]


# ---------------------------------------------------------------------------
# What this module deliberately does NOT check
# ---------------------------------------------------------------------------


class TestTheLoadersJob:
    """The §1 key charset and the ``repo:``/``remotes:`` TYPES are the
    manifest loader's, and it raises: such a document never loads, so it is
    unusable input rather than a finding list. Pinned here so the missing
    codes read as a decision, not as a gap.
    """

    @pytest.mark.parametrize("name", ["Widgets", f"{HOST}", "_lmer", "../escape"])
    def test_a_bad_short_name_never_reaches_this_module(self, name):
        doc = _doc()
        doc["projects"][name] = {"repo": "example.org/other/repo"}

        with pytest.raises(ManifestError):
            parse_manifest(_text(doc))

    @pytest.mark.parametrize("entry", [{"repo": 12345}, {"repo": pf.PRIMARY_REPO, "remotes": "origin"}])
    def test_a_bad_value_type_never_reaches_this_module(self, entry):
        doc = _entry(**entry)

        with pytest.raises(ManifestError):
            parse_manifest(_text(doc))

    def test_the_raising_path_stays_available_for_exit_two(self, schema_plexus):
        doc = _doc()
        doc["projects"]["fork"] = {"repo": pf.PRIMARY_REPO}
        schema_plexus.manifest.write_text(_text(doc), encoding="utf-8")

        with pytest.raises(ManifestError):
            load_registry(schema_plexus.root)


# ---------------------------------------------------------------------------
# Drift guard — one definition of normalization, one owner per rule
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tree():
    """The check module's AST — what the drift guard inspects."""
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))


class TestDriftGuard:
    """Asserted on the module SOURCE: a faithful private copy of a rule keeps
    the behaviour tests green and is exactly the drift this guards against
    (D21 for normalization, the wave-1/wave-2 layering decision for the
    identity rules — the loader and ``build_registry`` are the two layers,
    and this module is not a third).
    """

    @staticmethod
    def _imports(tree):
        """``(module, name)`` for every import; ``name`` is ``None`` for ``import x``."""
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.extend((alias.name, None) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                out.extend((node.module or "", alias.name) for alias in node.names)
        return out

    def test_no_url_parsing_of_its_own(self, tree):
        for module, name in self._imports(tree):
            assert module.split(".", 1)[0] not in {"re", "urllib"}, module
            assert name not in {"urlparse", "urlsplit", "normalize_repo_url"}, (module, name)

    def test_normalization_comes_from_the_one_adapter(self, tree):
        assert ("lmer_schema.urls", "normalize") in self._imports(tree)

    def test_the_identity_rules_are_not_restated(self, tree):
        # Nothing private is borrowed from the registry, and no charset or
        # duplicate-key rule is spelled again (the module docstring says who
        # owns each).
        borrowed = {
            name for module, name in self._imports(tree)
            if module == "lmer_schema.registry"
        }
        assert borrowed == {"Registry", "RegistryError", "build_registry"}
        source = MODULE_PATH.read_text(encoding="utf-8")
        assert "[a-z0-9]" not in source
        assert "SHORT_NAME_RE" not in source
