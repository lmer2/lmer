"""``lmer_schema.registry.resolve_project_dir`` — project-directory resolution (D9).

A session resolves its project dir by normalizing its repo URL through the
urls adapter and looking it up across the registry union; on a miss the
work goes to the ``{host}/{project}`` HOST BUCKET on disk — visibly unnamed,
never auto-named (D9). These tests, against the shared fixture trees, pin:

- registry hit via the primary repo and via an additional remote, in any
  §4 spelling, resolving to ``<short-name>/``;
- host-bucket fallback hit (the fixture's unregistered bucket);
- the §1 markers: a dotted top-level name is a bucket and never a short
  name, an invalid short-name charset is rejected, and a candidate dir is a
  project only if it is PROJECT-SHAPED (directly holds a schema or legacy
  area) — a namespace level or an empty dir is not;
- the documented miss value: ``how == MISS``, ``path is None``, and the
  bucket path where work WOULD be parked;
- PRECEDENCE, asserted not emergent: when a remote has both a registry
  entry and a real project-shaped bucket dir, the registry wins (SCHEMA.md
  "Resolution is registry first, bucket path second"; the split tree is the
  validator's error, not the resolver's);
- a legacy tree (no manifest) resolves by bucket only, and a broken manifest
  raises rather than demoting to bucket-only (D20).
"""

from pathlib import Path

import pytest
import yaml

from tests import plexus_fixtures as pf
from lmer_schema.manifest import ManifestError
from lmer_schema import registry as reg
from lmer_schema.registry import (
    BUCKET_HIT,
    MISS,
    PROJECT_MARKERS,
    REGISTRY_HIT,
    RegistryError,
    Resolution,
    bucket_path,
    is_host_bucket_name,
    is_project_shaped,
    is_short_name,
    load_registry,
    resolve_project_dir,
)
from lmer_schema.urls import lookup_key, normalize

HOST, PATH = pf.PRIMARY_REPO.split("/", 1)
BUCKET_URL = f"https://{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}.git"

# Fake token value (glpat- shape, not a real secret).
TOKEN = "glpat-FAKEtoken123"


def _make_project_shaped(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "_runs").mkdir()
    return path


def _register(tree, name, entry):
    """Add a registry entry to this test's OWN fixture manifest."""
    doc = yaml.safe_load(tree.manifest.read_text(encoding="utf-8"))
    doc["projects"][name] = entry
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# The result type and its constants
# ---------------------------------------------------------------------------


def test_how_constants_are_distinct_strings():
    assert {REGISTRY_HIT, BUCKET_HIT, MISS} == {"registry", "bucket", "miss"}


def test_project_markers_are_the_spec_list():
    # §1 / SCHEMA.md "Project-shaped directory": any schema area or any
    # legacy area, directly inside. Exactly these, no path-depth rule.
    assert set(PROJECT_MARKERS) == {
        "_runs", "_plans", "_agents", "_info", "_specs", "_taskdef", "_claims",
        "runs", "specs", "info", "memory",
    }


def test_the_bucket_walk_has_no_depth_rule():
    # SCHEMA.md Concepts and Host buckets both say the marker test decides a
    # project from a namespace level, "not path depth". A depth constant is
    # the shape of the rule this module must NOT have (!254 thread 2).
    assert not hasattr(reg, "BUCKET_MAX_DEPTH")
    assert "BUCKET_MAX_DEPTH" not in reg.__all__


def test_resolution_is_frozen(schema_plexus):
    result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
    with pytest.raises((AttributeError, TypeError)):
        result.how = MISS


# ---------------------------------------------------------------------------
# Registry hits
# ---------------------------------------------------------------------------


class TestRegistryHit:
    def test_via_primary_repo(self, schema_plexus):
        result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
        assert isinstance(result, Resolution)
        assert result.how == REGISTRY_HIT
        assert result.resolved
        assert result.path == schema_plexus.project_dir
        assert result.project == pf.PROJECT
        assert result.key == pf.PRIMARY_REPO

    @pytest.mark.parametrize("remote", pf.REMOTES)
    def test_via_an_additional_remote(self, schema_plexus, remote):
        result = resolve_project_dir(schema_plexus.root, remote)
        assert result.how == REGISTRY_HIT
        assert result.path == schema_plexus.project_dir
        assert result.project == pf.PROJECT
        assert result.key == remote

    @pytest.mark.parametrize(
        "spelling",
        [
            f"https://{HOST}/{PATH}.git",
            f"git@{HOST}:{PATH}.git",
            f"ssh://git@{HOST}:22/{PATH}",
            f"https://oauth2:{TOKEN}@{HOST}/{PATH}.git",
            f"https://{HOST.upper()}/{PATH.upper()}/",
        ],
    )
    def test_any_spelling_lands_in_the_one_tree(self, schema_plexus, spelling):
        # D8: multi-remote projects land in ONE tree with no symlinks.
        result = resolve_project_dir(schema_plexus.root, spelling)
        assert result.how == REGISTRY_HIT
        assert result.path == schema_plexus.project_dir
        assert TOKEN not in result.key
        assert TOKEN not in str(result.bucket_path)

    def test_hit_does_not_need_the_project_dir_to_exist(self, schema_plexus, tmp_path):
        # A registered project's dir is created on first write (SCHEMA.md
        # instance-root row); resolution answers where it IS, not whether.
        manifest = schema_plexus.manifest.read_text(encoding="utf-8")
        root = tmp_path / "fresh"
        root.mkdir()
        (root / pf.MANIFEST_NAME).write_text(manifest, encoding="utf-8")
        result = resolve_project_dir(root, pf.PRIMARY_REPO)
        assert result.how == REGISTRY_HIT
        assert result.path == root / pf.PROJECT
        assert not result.path.exists()

    def test_bucket_path_is_still_reported_on_a_hit(self, schema_plexus):
        # Where the same remote's bucket WOULD be — the validator's
        # split-tree check and migration tooling compare against it.
        result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
        assert result.bucket_path == schema_plexus.root / HOST / PATH

    def test_explicit_registry_is_used_as_given(self, schema_plexus):
        registry = load_registry(schema_plexus.root)
        result = resolve_project_dir(schema_plexus.root, pf.REMOTES[0], registry=registry)
        assert result.how == REGISTRY_HIT


# ---------------------------------------------------------------------------
# Host-bucket fallback
# ---------------------------------------------------------------------------


class TestBucketHit:
    def test_unregistered_repo_falls_back_to_its_bucket(self, schema_plexus):
        result = resolve_project_dir(schema_plexus.root, BUCKET_URL)
        assert result.how == BUCKET_HIT
        assert result.resolved
        assert result.path == schema_plexus.bucket_dir
        assert result.bucket_path == schema_plexus.bucket_dir
        # D9: no name is ever derived.
        assert result.project is None
        assert result.key == f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}"

    def test_bucket_is_never_misread_as_a_registry_hit(self, schema_plexus):
        registry = load_registry(schema_plexus.root)
        assert registry.lookup(BUCKET_URL) is None
        assert resolve_project_dir(schema_plexus.root, BUCKET_URL).how == BUCKET_HIT

    def test_bucket_path_keeps_the_written_path_case(self, tmp_path):
        # The bucket is where legacy lmer WROTE the tree: host lowered, path
        # case as written — a case-sensitive fs must find it. The registry
        # key is the case-folded comparison form.
        root = tmp_path / "plexus"
        bucket = _make_project_shaped(root / "git.example.org" / "Acme" / "Widgets")
        result = resolve_project_dir(root, "https://GIT.EXAMPLE.ORG/Acme/Widgets.git")
        assert result.how == BUCKET_HIT
        assert result.path == bucket
        assert result.key == "git.example.org/acme/widgets"

    def test_legacy_shaped_bucket_counts(self, tmp_path):
        root = tmp_path / "plexus"
        bucket = root / "git.example.org" / "acme" / "widgets"
        (bucket / "runs").mkdir(parents=True)
        assert resolve_project_dir(root, "git.example.org/acme/widgets").path == bucket

    def test_a_ported_remote_uses_the_unported_bucket(self, tmp_path):
        # Superseded contract (!254 thread 1): the port makes a different
        # registry KEY, never a different DIRECTORY — the directory is the
        # one lmer writes, and a `host:2222/` tree is one nothing wrote.
        # TestPortedRemoteBucket below is the full statement.
        root = tmp_path / "plexus"
        bucket = _make_project_shaped(root / "git.example.org" / "acme" / "widgets")
        result = resolve_project_dir(root, "ssh://git@git.example.org:2222/acme/widgets")
        assert result.how == BUCKET_HIT
        assert result.path == bucket


# ---------------------------------------------------------------------------
# The §1 markers
# ---------------------------------------------------------------------------


class TestDottedBucketIsNotAShortName:
    def test_bucket_host_is_not_a_short_name(self):
        assert not is_short_name(pf.BUCKET_HOST)
        assert is_host_bucket_name(pf.BUCKET_HOST)

    @pytest.mark.parametrize("name", ["lmer", "q3-capacity", "localhost", "gitlab"])
    def test_dotless_names_are_never_buckets(self, name):
        # A dotless host is unsupported as a bucket at v1 (§1): register it.
        assert not is_host_bucket_name(name)

    @pytest.mark.parametrize("name", [".git", ".hidden", "_agents", "_knowledge", ".", ".."])
    def test_hidden_and_schema_names_are_never_buckets(self, name):
        assert not is_host_bucket_name(name)

    def test_dotted_registry_name_is_rejected(self, schema_plexus):
        # Registering a project under a dotted name would make the dir
        # look like a bucket (§1): resolution refuses it outright. The
        # loader owns the charset for a document read from disk and the
        # registry re-checks it, so the contract asserted here is the
        # taxonomy both share (ManifestError, D20) — see
        # test_lmer_schema_registry.TestLayering for the ownership itself.
        _register(schema_plexus, pf.BUCKET_HOST, {"repo": "example.org/x/y"})
        with pytest.raises(ManifestError):
            resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)

    def test_short_name_dir_is_never_read_as_a_bucket(self, schema_plexus):
        # `lmer/_runs` exists on disk and is dotless: a lookup that would
        # spell it as {host}/{path} must not resolve there.
        assert (schema_plexus.project_dir / "_runs").is_dir()
        result = resolve_project_dir(schema_plexus.root, f"{pf.PROJECT}/_runs")
        assert result.how == MISS
        assert result.bucket_path is None

    def test_dotless_host_has_no_bucket(self, tmp_path):
        root = tmp_path / "plexus"
        _make_project_shaped(root / "localhost" / "acme" / "widgets")
        result = resolve_project_dir(root, "https://localhost/acme/widgets")
        assert result.how == MISS
        assert result.bucket_path is None


class TestShortNameCharset:
    @pytest.mark.parametrize(
        "name", ["Widgets", "git.example.com", "_lmer", "-x", "a/b", "a b"]
    )
    def test_invalid_registry_name_fails_resolution_loudly(self, schema_plexus, name):
        # Loudly = raises the D20 taxonomy naming the rule, never a MISS
        # that would route the work somewhere else.
        _register(schema_plexus, name, {"repo": "example.org/x/y"})
        with pytest.raises(ManifestError) as excinfo:
            resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
        assert "[a-z0-9][a-z0-9_-]*" in str(excinfo.value)

    def test_fixture_names_are_valid(self, schema_plexus):
        for name in load_registry(schema_plexus.root):
            assert is_short_name(name)


class TestProjectShaped:
    @pytest.mark.parametrize("marker", PROJECT_MARKERS)
    def test_each_marker_makes_a_project(self, tmp_path, marker):
        candidate = tmp_path / "candidate"
        (candidate / marker).mkdir(parents=True)
        assert is_project_shaped(candidate)

    def test_fixture_dirs_are_project_shaped(self, schema_plexus, legacy_plexus):
        assert is_project_shaped(schema_plexus.project_dir)
        assert is_project_shaped(schema_plexus.repoless_dir)
        assert is_project_shaped(schema_plexus.bucket_dir)
        assert is_project_shaped(legacy_plexus.project_dir)

    def test_marker_must_be_a_directory(self, tmp_path):
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "_runs").write_text("not a dir\n", encoding="utf-8")
        assert not is_project_shaped(candidate)

    def test_marker_must_be_direct(self, tmp_path):
        # A namespace level ABOVE a project is not a project (no depth rule;
        # the marker has to sit directly inside).
        candidate = tmp_path / "candidate"
        (candidate / "sub" / "_runs").mkdir(parents=True)
        assert not is_project_shaped(candidate)

    def test_empty_missing_and_file_are_not_project_shaped(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert not is_project_shaped(empty)
        assert not is_project_shaped(tmp_path / "missing")
        file = tmp_path / "file"
        file.write_text("x\n", encoding="utf-8")
        assert not is_project_shaped(file)

    def test_namespace_level_inside_a_bucket_is_rejected(self, schema_plexus):
        # {host}/unnamed/ exists (it holds the widget bucket) but is a
        # namespace level, not a project: a lookup for it is a MISS.
        namespace = schema_plexus.bucket_dir.parent
        assert namespace.is_dir()
        assert not is_project_shaped(namespace)
        result = resolve_project_dir(schema_plexus.root, f"{pf.BUCKET_HOST}/{namespace.name}")
        assert result.how == MISS
        assert result.path is None
        assert result.bucket_path == namespace

    def test_existing_but_empty_bucket_dir_is_rejected(self, schema_plexus):
        empty = schema_plexus.root / pf.BUCKET_HOST / "unnamed" / "empty"
        empty.mkdir()
        result = resolve_project_dir(schema_plexus.root, f"{pf.BUCKET_HOST}/unnamed/empty")
        assert result.how == MISS
        assert result.bucket_path == empty


# ---------------------------------------------------------------------------
# The miss value
# ---------------------------------------------------------------------------


class TestMiss:
    def test_unresolvable_lookup(self, schema_plexus):
        result = resolve_project_dir(schema_plexus.root, "https://example.org/nobody/home.git")
        assert result == Resolution(
            how=MISS,
            path=None,
            project=None,
            key="example.org/nobody/home",
            bucket_path=schema_plexus.root / "example.org" / "nobody" / "home",
        )
        assert not result.resolved

    def test_miss_reports_where_work_would_be_parked(self, schema_plexus):
        # D9: on a miss a session MUST write to the bucket — the resolver
        # says where, and does not create it.
        result = resolve_project_dir(schema_plexus.root, "https://example.org/nobody/home.git")
        assert not result.bucket_path.exists()
        assert result.bucket_path == schema_plexus.root / "example.org" / "nobody" / "home"
        assert schema_plexus.root in result.bucket_path.parents

    @pytest.mark.parametrize("url", ["", None, "   ", "https://", "just-a-word"])
    def test_nothing_to_look_up(self, schema_plexus, url):
        result = resolve_project_dir(schema_plexus.root, url)
        assert result.how == MISS
        assert result.path is None
        assert result.bucket_path is None

    @pytest.mark.parametrize("deep", ["a/b/c/d", "a/b/c/d/e", "a/b/c/d/e/f"])
    def test_a_project_resolves_at_any_depth(self, tmp_path, deep):
        # The marker test decides, never depth: a subgroup tree one level
        # deeper than the live work repo's deepest resolves like a shallow
        # one (!254 thread 2). The old four-level cap made this a MISS.
        root = tmp_path / "plexus"
        _make_project_shaped(root.joinpath("git.example.org", *deep.split("/")))
        result = resolve_project_dir(root, f"https://git.example.org/{deep}")
        assert result.how == BUCKET_HIT
        assert result.path == root.joinpath("git.example.org", *deep.split("/"))
        assert result.bucket_path == result.path

    def test_a_miss_still_names_the_bucket_at_full_depth(self, tmp_path):
        # D9 ("MUST write, MUST NOT refuse or lose the work") needs a path
        # even when nothing is on disk yet, at whatever depth the remote is.
        root = tmp_path / "plexus"
        deep = "a/b/c/d/e/f"
        result = resolve_project_dir(root, f"https://git.example.org/{deep}")
        assert result.how == MISS
        assert result.path is None
        assert result.bucket_path == root.joinpath("git.example.org", *deep.split("/"))

    @pytest.mark.parametrize("spelling", ["git.example.org/../etc", "git.example.org/a/./b", "git.example.org//a"])
    def test_bucket_path_never_escapes_or_hops(self, tmp_path, spelling):
        assert bucket_path(tmp_path, spelling) is None


# ---------------------------------------------------------------------------
# A non-default port: in the KEY, never in a directory name
# ---------------------------------------------------------------------------


class TestPortedRemoteBucket:
    """§4 gives a non-default port its own registry KEY. The DIRECTORY it
    names is the one lmer already wrote, and lmer derives that with
    ``urlparse(...).hostname`` — the port dropped (!254 thread 1)."""

    PORTED = (
        "ssh://git@gitlab.example.com:2222/acme/widgets",
        "https://gitlab.example.com:8443/acme/widgets",
    )
    WROTE = ("gitlab.example.com", "acme", "widgets")

    @pytest.mark.parametrize("url", PORTED)
    def test_the_bucket_directory_drops_the_port(self, tmp_path, url):
        assert bucket_path(tmp_path, normalize(url)) == tmp_path.joinpath(*self.WROTE)

    @pytest.mark.parametrize("url", PORTED)
    def test_the_tree_lmer_wrote_is_found(self, tmp_path, url):
        root = tmp_path / "plexus"
        _make_project_shaped(root.joinpath(*self.WROTE))
        result = resolve_project_dir(root, url)
        assert result.how == BUCKET_HIT
        assert result.path == root.joinpath(*self.WROTE)

    @pytest.mark.parametrize("url", PORTED)
    def test_the_key_still_keeps_the_port(self, url):
        # The asymmetry, pinned: two ports are two repos in the registry and
        # one directory on disk.
        assert ":" in lookup_key(url).partition("/")[0]

    @pytest.mark.parametrize("url", PORTED)
    def test_agrees_with_the_function_that_writes_the_path(self, tmp_path, url):
        # The authority for legacy placement is the launch path itself: what
        # `_parse_host_project` returns becomes LMER_REPO_HOST/PROJECT and
        # therefore every {host}/{project} directory in the work repo.
        from lmer_cli.container.clone_and_exec import _parse_host_project

        host, project = _parse_host_project(url)
        assert bucket_path(tmp_path, normalize(url)) == tmp_path.joinpath(
            host, *project.split("/")
        )


class TestSchema2BucketIdentity:
    def test_non_default_ports_have_distinct_filesystem_addresses(self, tmp_path):
        first = resolve_project_dir(
            tmp_path,
            "ssh://git@git.example.org:2222/Acme/Widgets.git",
            registry=None,
            schema=2,
        )
        second = resolve_project_dir(
            tmp_path,
            "ssh://git@git.example.org:2200/Acme/Widgets.git",
            registry=None,
            schema=2,
        )

        assert first.bucket_path == tmp_path / "git.example.org%3A2222" / "Acme" / "Widgets"
        assert second.bucket_path == tmp_path / "git.example.org%3A2200" / "Acme" / "Widgets"
        assert first.bucket_path != second.bucket_path

    def test_path_case_variants_have_distinct_real_temp_tree_addresses(self, tmp_path):
        upper = resolve_project_dir(
            tmp_path, "https://git.example.org/Acme/Widgets.git",
            registry=None, schema=2,
        )
        lower = resolve_project_dir(
            tmp_path, "https://git.example.org/acme/widgets.git",
            registry=None, schema=2,
        )
        _make_project_shaped(upper.bucket_path)
        _make_project_shaped(lower.bucket_path)

        assert upper.bucket_path != lower.bucket_path
        assert resolve_project_dir(
            tmp_path, "https://git.example.org/Acme/Widgets.git",
            registry=None, schema=2,
        ).path == upper.bucket_path
        assert resolve_project_dir(
            tmp_path, "https://git.example.org/acme/widgets.git",
            registry=None, schema=2,
        ).path == lower.bucket_path

    def test_existing_schema_1_bucket_makes_ported_schema_2_placement_ambiguous(
            self, tmp_path):
        _make_project_shaped(tmp_path / "git.example.org" / "Acme" / "Widgets")

        with pytest.raises(RegistryError, match="ambiguous"):
            resolve_project_dir(
                tmp_path,
                "ssh://git@git.example.org:2222/Acme/Widgets.git",
                registry=None,
                schema=2,
            )

    def test_registered_project_is_authoritative_over_legacy_bucket(self, tmp_path):
        (tmp_path / "_lmer.yaml").write_text(
            yaml.safe_dump({
                "schema": 2,
                "name": "test",
                "projects": {
                    "widgets": {
                        "repo": "ssh://git@git.example.org:2222/Acme/Widgets.git",
                    },
                },
            }),
            encoding="utf-8",
        )
        _make_project_shaped(tmp_path / "widgets")
        _make_project_shaped(tmp_path / "git.example.org" / "Acme" / "Widgets")

        result = resolve_project_dir(
            tmp_path, "ssh://git@git.example.org:2222/Acme/Widgets.git",
        )

        assert result.how == REGISTRY_HIT
        assert result.path == tmp_path / "widgets"
        assert result.bucket_path == (
            tmp_path / "git.example.org%3A2222" / "Acme" / "Widgets"
        )

    def test_schema_1_compatibility_symlink_is_not_an_ambiguous_real_bucket(
            self, tmp_path):
        target = _make_project_shaped(tmp_path / "widgets")
        legacy = tmp_path / "git.example.org" / "Acme" / "Widgets"
        legacy.parent.mkdir(parents=True)
        legacy.symlink_to(target)

        result = resolve_project_dir(
            tmp_path,
            "ssh://git@git.example.org:2222/Acme/Widgets.git",
            registry=None,
            schema=2,
        )

        assert result.how == MISS
        assert result.bucket_path == (
            tmp_path / "git.example.org%3A2222" / "Acme" / "Widgets"
        )

    def test_uninspectable_parent_fails_closed(self, tmp_path, monkeypatch):
        (tmp_path / "git.example.org").mkdir()
        real_iterdir = Path.iterdir

        def refuse_root_inspection(path):
            if path == tmp_path:
                raise PermissionError("test denies directory inspection")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", refuse_root_inspection)

        with pytest.raises(RegistryError, match="cannot inspect"):
            resolve_project_dir(
                tmp_path,
                "https://git.example.org/Acme/Widgets.git",
                registry=None,
                schema=2,
            )


# ---------------------------------------------------------------------------
# Precedence — registry first, bucket second (asserted, not emergent)
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_registry_wins_over_a_real_bucket_dir(self, schema_plexus):
        # Both exist: the registry entry AND a real, project-shaped
        # {host}/{project}/ dir for the same remote (the split tree the
        # validator flags). Resolution is registry first (SCHEMA.md "Host
        # buckets": "Resolution is registry first, bucket path second").
        split = _make_project_shaped(schema_plexus.root / HOST / PATH)
        assert is_project_shaped(split)
        assert resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO, registry=None).path == split

        result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
        assert result.how == REGISTRY_HIT
        assert result.path == schema_plexus.project_dir
        assert result.bucket_path == split

    def test_registry_wins_via_an_additional_remote_too(self, schema_plexus):
        host, path = pf.REMOTES[0].split("/", 1)
        _make_project_shaped(schema_plexus.root / host / path)
        result = resolve_project_dir(schema_plexus.root, pf.REMOTES[0])
        assert result.how == REGISTRY_HIT
        assert result.path == schema_plexus.project_dir

    def test_compat_symlink_at_the_old_path_is_fine(self, schema_plexus):
        # §9 migration recipe: a symlink {host}/{project} -> <short-name>.
        old = schema_plexus.root / HOST / PATH
        old.parent.mkdir(parents=True)
        old.symlink_to("../../" + pf.PROJECT)
        result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO)
        assert result.how == REGISTRY_HIT
        assert result.path == schema_plexus.project_dir


# ---------------------------------------------------------------------------
# Legacy tree: bucket-only
# ---------------------------------------------------------------------------


class TestLegacyTree:
    def test_resolves_by_bucket_only(self, legacy_plexus):
        assert legacy_plexus.manifest is None
        result = resolve_project_dir(legacy_plexus.root, pf.PRIMARY_REPO)
        assert result.how == BUCKET_HIT
        assert result.path == legacy_plexus.project_dir
        assert result.project is None

    def test_any_spelling_of_the_primary(self, legacy_plexus):
        result = resolve_project_dir(legacy_plexus.root, f"git@{HOST}:{PATH}.git")
        assert result.path == legacy_plexus.project_dir

    @pytest.mark.parametrize("remote", pf.REMOTES)
    def test_additional_remotes_do_not_resolve_without_a_registry(self, legacy_plexus, remote):
        # No registry -> no union: a mirror URL is just another unnamed repo.
        result = resolve_project_dir(legacy_plexus.root, remote)
        assert result.how == MISS
        host, path = remote.split("/", 1)
        assert result.bucket_path == legacy_plexus.root / host / path

    def test_explicit_none_registry_forces_bucket_only(self, schema_plexus):
        result = resolve_project_dir(schema_plexus.root, pf.PRIMARY_REPO, registry=None)
        assert result.how == MISS
        assert result.project is None


def test_broken_manifest_never_demotes_to_bucket_only(schema_plexus):
    # D20: a present-but-broken manifest fails loud; it is never read as
    # "no registry, fall back to the bucket".
    schema_plexus.manifest.write_text("schema: [\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        resolve_project_dir(schema_plexus.root, BUCKET_URL)
