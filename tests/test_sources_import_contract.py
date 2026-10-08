"""Import contract for src/lmer_cli/container/sources.py.

sources.py must be loadable two ways (see its module docstring):

1. Standalone, via the spec_from_file_location recipe clone_and_exec uses —
   with the lmer_cli package never imported, under PYTHONSAFEPATH (-P), and
   with a decoy ``sources.py`` in the cwd (proving no shadowing).
2. As the normal package module ``lmer_cli.container.sources``.

Plus the PyYAML guard (spec N5): the module imports cleanly without PyYAML,
and load_sources() on an *existing* sources.yaml without PyYAML raises the
refuse-start error instead of silently skipping.

Plus the manifest tier (issue #370): a tiered load_sources() call takes the
manifest's ``sources:`` as a dict the caller already parsed, so it needs
neither lmer_schema (the one manifest parser) nor PyYAML — the standalone
copy resolves it with both absent and imports nothing new.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lmer_cli.container import sources

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "src" / "lmer_cli" / "container" / "sources.py"

# The loader recipe from the sources.py docstring, inlined the way
# clone_and_exec will carry it. Kept as a here-doc so the subprocess runs it
# with -P (PYTHONSAFEPATH) and an unrelated cwd, exactly like the container
# entrypoint hardened case.
_STANDALONE_LOADER = textwrap.dedent(
    """
    import importlib.util
    import sys
    from pathlib import Path

    assert "lmer_cli" not in sys.modules, "lmer_cli leaked in before load"

    def _load_sources_module():
        # Load the sibling sources.py without importing lmer_cli.
        name = "lmer_container_sources"
        if name in sys.modules:
            return sys.modules[name]
        path = Path({sources_path!r})
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    mod = _load_sources_module()
    """
)


def _run_standalone(extra_body, cwd, prelude=""):
    """Run the standalone loader + extra assertions in a -P subprocess."""
    script = (
        prelude
        + _STANDALONE_LOADER.format(sources_path=str(SOURCES_PATH))
        + textwrap.dedent(extra_body)
    )
    return subprocess.run(
        [sys.executable, "-P", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_standalone_load_without_lmer_cli(tmp_path):
    """The recipe loads sources.py with lmer_cli absent, under -P, unshadowed."""
    # Decoy: a stray sources.py in the cwd must never win over the sibling
    # file loaded by absolute path.
    (tmp_path / "sources.py").write_text("raise RuntimeError('decoy imported')\n")

    result = _run_standalone(
        """
        assert mod.__doc__ and "Import contract" in mod.__doc__
        assert mod.STANDALONE_MODULE_NAME == "lmer_container_sources"
        assert issubclass(mod.SourcesConfigError, Exception)
        assert callable(mod.load_sources)
        # Idempotent: a second call returns the cached module object.
        assert _load_sources_module() is mod
        # The load pulled in neither the package nor a generic name.
        assert "lmer_cli" not in sys.modules, "standalone load imported lmer_cli"
        assert "sources" not in sys.modules, "generic 'sources' name was squatted"
        print("OK")
        """,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_standalone_load_survives_missing_pyyaml(tmp_path):
    """Module imports cleanly without PyYAML; parsing a present file refuses."""
    (tmp_path / "sources.yaml").write_text("schema: 1\n")
    result = _run_standalone(
        """
        assert mod.yaml is None, "yaml import should have been blocked"
        # Absent file: silent legacy, no PyYAML needed.
        cfg, warnings = mod.load_sources({missing!r})
        assert cfg is None and warnings == []
        # Present file without PyYAML: loud refuse-start, never a skip.
        try:
            mod.load_sources({present!r})
        except mod.SourcesConfigError as exc:
            assert "PyYAML" in str(exc)
        else:
            raise AssertionError("load_sources did not refuse without PyYAML")
        print("OK")
        """.format(
            missing=str(tmp_path / "no-such-sources.yaml"),
            present=str(tmp_path / "sources.yaml"),
        ),
        cwd=tmp_path,
        # None in sys.modules makes any later `import yaml` raise
        # ImportError, simulating an interpreter without PyYAML.
        prelude="import sys\nsys.modules['yaml'] = None\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_standalone_manifest_tier_call_needs_neither_lmer_schema_nor_pyyaml(tmp_path):
    """A manifest-tier call resolves standalone with lmer_schema and PyYAML absent."""
    result = _run_standalone(
        """
        assert mod.yaml is None, "yaml import should have been blocked"
        work_url = "https://git.example.com/20c/worklog.git"
        manifest = dict(taskdef=dict(repo="https://git.example.com/agents/taskdefs.git", ref="main"))
        cfg, warnings = mod.load_sources(
            {missing!r},
            work_repo_url=work_url,
            manifest_sources=manifest,
            manifest_path="/work/_plexus.yaml",
        )
        assert warnings == [], warnings
        assert cfg["sources"] == dict(taskdef=dict(repo="https://git.example.com/agents/taskdefs.git", ref="main")), cfg
        assert cfg["tiers"] == dict(taskdef=mod.TIER_MANIFEST), cfg
        assert mod.declared_origin_label(mod.TIER_MANIFEST) == "declared (manifest)"
        assert mod.declared_origin_label(mod.TIER_LEGACY) == "declared (sources.yaml)"
        # The trust rules fire through the same standalone path.
        try:
            mod.load_sources(
                {missing!r},
                work_repo_url=work_url,
                manifest_sources=dict(napkin=dict(repo="https://evil.example.org/20c/napkin.git")),
                manifest_path="/work/_plexus.yaml",
            )
        except mod.SourcesConfigError as exc:
            assert "LMER_NAPKIN_REPO" in str(exc), exc
        else:
            raise AssertionError("manifest-tier trust rule did not fire")
        # Nothing new was imported: not the manifest parser, not the package,
        # not PyYAML (the None sentinels planted by the prelude are blocks,
        # not imports).
        loaded = sorted(
            name
            for name, module in sys.modules.items()
            if name.split(".")[0] in ("lmer_schema", "lmer_cli", "yaml") and module is not None
        )
        assert loaded == [], loaded
        print("OK")
        """.format(missing=str(tmp_path / "no-such-sources.yaml")),
        cwd=tmp_path,
        # None in sys.modules makes any later import of the name raise
        # ImportError: an interpreter with neither PyYAML nor lmer_schema.
        prelude="import sys\nsys.modules['yaml'] = None\nsys.modules['lmer_schema'] = None\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_package_import_works():
    """The normal package import path exposes the same surface."""
    assert sources.__doc__ and "Import contract" in sources.__doc__
    assert sources.STANDALONE_MODULE_NAME == "lmer_container_sources"
    assert issubclass(sources.SourcesConfigError, Exception)


def test_package_load_sources_absent_file_is_silent_legacy(tmp_path):
    cfg, warnings = sources.load_sources(tmp_path / "sources.yaml")
    assert cfg is None
    assert warnings == []


def test_package_load_sources_missing_pyyaml_refuses(tmp_path, monkeypatch):
    (tmp_path / "sources.yaml").write_text("schema: 1\n")
    monkeypatch.setattr(sources, "yaml", None)
    with pytest.raises(sources.SourcesConfigError, match="PyYAML"):
        sources.load_sources(tmp_path / "sources.yaml")


# ---------------------------------------------------------------------------
# The manifest-tier capability marker (issue #370, E.probe)
# ---------------------------------------------------------------------------


def test_the_manifest_tier_marker_is_true_in_the_package():
    """`work migrate --instance`'s version floor reads this constant."""
    assert sources.MANIFEST_TIER_SUPPORTED is True


def test_the_manifest_tier_marker_survives_the_standalone_load(tmp_path):
    result = _run_standalone(
        "assert mod.MANIFEST_TIER_SUPPORTED is True\nprint('OK')\n", tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_the_marker_is_honest_about_a_manifest_only_tree(tmp_path):
    """The marker may only be true while the container seam actually reads
    the manifest tier: drive the seam on a manifest-only tree and require the
    declared clone to be planned."""
    from tests.test_clone_and_exec_sources import (
        SOURCES_BLOCK,
        _manifest,
        _recording_clone,
        _resolve,
        _write_tree,
    )

    _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
    calls = []
    rc, _env = _resolve(
        tmp_path, {"LMER_WORK_REPO_PATH": "/work"}, clone=_recording_clone(calls)
    )
    assert rc == 0
    assert calls, "a manifest-only declaration must still plan its clone"
