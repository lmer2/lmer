"""Tests for the sources.yaml seam in clone_and_exec (#105).

Covers the guarded PyYAML import (the entrypoint is stdlib-only except for
that one sanctioned import; a present config that cannot be read must never
be silently ignored — refuse start, exit 2) and the container-side
resolution wired between the work-repo clone and clone_aux_repos:

- the five-row declared/env matrix, per source (taskdef, napkin) and per
  field (repo; ref for taskdef only) — _resolve_sources_matrix
- the prompt layer (mismatch pick, continue-in-legacy) driven by a fake TTY
- propagation (spec N1): resolved env vars, the /taskdef LMER_TASKDEF_PATHS
  append, the LMER_NAPKIN_PATH separate-mode recompute
- the loud declared-source clone failure path (spec N3), including
  continue-legacy reverting that source's propagation mutations
- credential redaction of every prompt/error/banner line
- zero-output silent legacy mode (spec G5)
- the manifest tier (issue #370, "Manifest tier" section at the end): a
  present ``_plexus.yaml`` is the second declaration file, read through the
  core's one manifest parser and handed to the standalone loader; env beats
  manifest beats legacy; a tree without a manifest keeps every byte of
  today's output and never imports the core
"""

import io
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.container import sources as sources_mod
from lmer_schema import manifest as manifest_core


def _with_yaml_available():
    """Patch context making ``import yaml`` succeed regardless of the test env
    (a stub module in sys.modules — the seam only needs importability)."""
    return patch.dict(sys.modules, {"yaml": types.ModuleType("yaml")})


def _with_yaml_missing():
    """Patch context making ``import yaml`` raise ImportError (None in
    sys.modules), exercising the real guarded-import branch."""
    return patch.dict(sys.modules, {"yaml": None})


class TestImportYaml:
    def test_returns_module_when_available(self):
        with _with_yaml_available():
            mod = clone_and_exec._import_yaml()
            assert mod is not None
            assert mod.__name__ == "yaml"

    def test_reports_unavailable_instead_of_raising(self):
        with _with_yaml_missing():
            assert clone_and_exec._import_yaml() is None


class TestRefuseStartIfSourcesUnreadable:
    def test_import_available_no_refusal(self, tmp_path, capsys):
        """sources.yaml present + PyYAML importable → proceed, no output."""
        (tmp_path / "sources.yaml").write_text("schema: 1\n")
        with _with_yaml_available():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 0
        assert capsys.readouterr().err == ""

    def test_import_missing_with_config_refuses_start(self, tmp_path, capsys):
        """PyYAML missing + sources.yaml present → exit code 2 and a
        remediation message naming the file, PyYAML, and the fix."""
        (tmp_path / "sources.yaml").write_text("schema: 1\n")
        with _with_yaml_missing():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 2
        err = capsys.readouterr().err
        assert "sources.yaml" in err
        assert "PyYAML" in err
        # Remediation: the operator's fix is a rebuild.
        assert "lmer build" in err

    def test_import_missing_without_config_is_silent(self, tmp_path, capsys):
        """PyYAML missing + no sources.yaml → silent legacy mode, unchanged
        behavior (the entrypoint stays runnable stdlib-only)."""
        with _with_yaml_missing():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 0
        assert capsys.readouterr().err == ""

    def test_refusal_error_passes_through_credential_scrub(self, tmp_path, capsys):
        """The refusal message is routed through _scrub_credentials, so it can
        never leak URL credentials (a filesystem path cannot carry a
        ``://user:token@`` form today, but the wrapping keeps the print
        token-safe if the message ever grows a URL)."""
        (tmp_path / "sources.yaml").write_text("schema: 1\n")
        scrubbed_inputs = []
        real_scrub = clone_and_exec._scrub_credentials

        def recording_scrub(text):
            scrubbed_inputs.append(text)
            return real_scrub(text)

        with _with_yaml_missing(), patch.object(
            clone_and_exec, "_scrub_credentials", recording_scrub
        ):
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 2
        err = capsys.readouterr().err
        assert any("sources.yaml" in text for text in scrubbed_inputs), (
            "the refusal message must be wrapped in _scrub_credentials"
        )
        assert "sources.yaml" in err


# --- Resolution matrix / prompt / propagation fixtures -----------------------

TOKEN = "sekret-token"
WORK_URL = f"https://oauth2:{TOKEN}@git.example.com/org/work.git"
WORK_URL_ANON = "https://git.example.com/org/work.git"
DECLARED_TASKDEF = "https://git.example.com/org/taskdefs.git"
DECLARED_NAPKIN = "https://git.example.com/org/napkin.git"
# The credentialed form of DECLARED_TASKDEF (normalized-equal → env wins).
ENV_TASKDEF_EQUAL = f"https://oauth2:{TOKEN}@git.example.com/org/taskdefs.git"
# A different same-host repo (normalized-different → mismatch row).
ENV_TASKDEF_OTHER = f"https://oauth2:{TOKEN}@git.example.com/org/other.git"
ENV_NAPKIN_OTHER = f"https://oauth2:{TOKEN}@git.example.com/org/other-napkin.git"


def _cfg(taskdef=None, napkin=None):
    srcs = {}
    if taskdef:
        srcs["taskdef"] = taskdef
    if napkin:
        srcs["napkin"] = napkin
    return {"schema": 1, "sources": srcs}


def _no_input(prompt=""):
    raise AssertionError(f"input() must not be reached here (prompt: {prompt!r})")


def _scripted_input(answers):
    """A fake-TTY input function returning *answers* in order."""
    it = iter(answers)

    def fake_input(prompt=""):
        return next(it)

    return fake_input


def _matrix(config, env, isatty=False, work_url=WORK_URL, input_fn=_no_input):
    return clone_and_exec._resolve_sources_matrix(
        config, env, isatty, work_url, sources_mod, input_fn=input_fn
    )


class TestMatrixRepoRows:
    """The five matrix rows for the repo field, per source."""

    def test_declared_only_taskdef_wins_and_propagates(self):
        """Row 1 (declared, env unset): declaration wins; the clone URL
        derives the work-repo credential; propagation sets the env var and
        appends /taskdef to LMER_TASKDEF_PATHS (spec N1)."""
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_PATHS": "/mnt/td0"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env)
        assert result["exit_code"] == 0
        assert result["origins"]["taskdef"] == "declared"
        assert result["env_updates"]["LMER_TASKDEF_REPO"] == ENV_TASKDEF_EQUAL
        assert result["env_updates"]["LMER_TASKDEF_PATHS"] == "/mnt/td0:/taskdef"
        (clone,) = result["clones"]
        assert clone["dest"] == "/taskdef"
        assert clone["url"] == ENV_TASKDEF_EQUAL
        assert clone["ref"] is None
        assert result["banners"] == [f"source taskdef: {DECLARED_TASKDEF} (declared)"]
        # The matrix itself mutates nothing.
        assert "LMER_TASKDEF_REPO" not in env

    def test_declared_only_napkin_recomputes_separate_mode(self):
        """Row 1 for napkin: LMER_NAPKIN_REPO set and LMER_NAPKIN_PATH
        recomputed to /napkin so setup_napkin_and_links links ~/napkin at
        the separate clone (spec N1)."""
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_NAPKIN_PATH": "/work/napkin"}
        result = _matrix(_cfg(napkin={"repo": DECLARED_NAPKIN}), env)
        assert result["origins"]["napkin"] == "declared"
        assert (
            result["env_updates"]["LMER_NAPKIN_REPO"]
            == f"https://oauth2:{TOKEN}@git.example.com/org/napkin.git"
        )
        assert result["env_updates"]["LMER_NAPKIN_PATH"] == "/napkin"
        (clone,) = result["clones"]
        assert clone["dest"] == "/napkin"
        assert result["banners"] == [f"source napkin: {DECLARED_NAPKIN} (declared)"]

    def test_declared_ssh_work_repo_derives_ssh_form(self):
        """Row 1 with an SSH-form work repo: the declared URL converts to
        the same SSH form (key auth just proved itself on the work clone)."""
        env = {}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            work_url="git@git.example.com:org/work.git",
        )
        (clone,) = result["clones"]
        assert clone["url"] == "git@git.example.com:org/taskdefs.git"
        assert clone["derived_mode"] == sources_mod.DERIVED_SSH

    def test_normalized_equal_env_wins_silently(self):
        """Row 2: env is the credentialed form of the declared repo — it
        wins with no propagation and no chatter, only the banner. The
        origin is env-match, not env-override: a matching env value is not
        an override, and the distinct label keeps the banner in agreement
        with the --show-env display (review !170)."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env)
        assert result["origins"]["taskdef"] == "env-match"
        assert result["env_updates"] == {}
        assert result["notes"] == []
        (clone,) = result["clones"]
        assert clone["url"] == ENV_TASKDEF_EQUAL
        assert result["banners"] == [
            f"source taskdef: {DECLARED_TASKDEF} (env-match)"
        ]

    def test_mismatch_headless_exits_2_with_scrubbed_values(self):
        """Row 3 headless: exit code 2, both values named credential-scrubbed,
        no clones and no propagation planned."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env, isatty=False)
        assert result["exit_code"] == 2
        (error,) = result["errors"]
        assert "git.example.com/org/taskdefs.git" in error
        assert "git.example.com/org/other.git" in error
        assert "LMER_TASKDEF_REPO" in error
        assert TOKEN not in error
        assert result["clones"] == []
        assert result["env_updates"] == {}

    def test_mismatch_headless_reports_both_sources(self):
        """Headless conflicts are reported for every source in one pass."""
        env = {
            "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER,
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
        }
        result = _matrix(
            _cfg(
                taskdef={"repo": DECLARED_TASKDEF},
                napkin={"repo": DECLARED_NAPKIN},
            ),
            env,
            isatty=False,
        )
        assert result["exit_code"] == 2
        assert len(result["errors"]) == 2

    def test_mismatch_tty_pick_declared(self, capsys):
        """Row 3 interactive, user picks the declaration: origin declared,
        propagation planned, prompt output scrubbed."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            isatty=True,
            input_fn=_scripted_input(["d"]),
        )
        assert result["exit_code"] == 0
        assert result["origins"]["taskdef"] == "declared"
        assert result["env_updates"]["LMER_TASKDEF_REPO"] == ENV_TASKDEF_EQUAL
        err = capsys.readouterr().err
        assert "mismatch" in err
        assert TOKEN not in err

    def test_mismatch_tty_pick_env(self):
        """Row 3 interactive, user keeps the env value: origin env-override,
        no propagation, env URL cloned."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            isatty=True,
            input_fn=_scripted_input(["e"]),
        )
        assert result["origins"]["taskdef"] == "env-override"
        assert result["env_updates"] == {}
        (clone,) = result["clones"]
        assert clone["url"] == ENV_TASKDEF_OTHER

    def test_env_only_notes_missing_declaration(self):
        """Row 4: env set, no declaration — env value used with a one-line
        note and an env-only banner; no loud clone (legacy aux behavior)."""
        env = {"LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env)
        assert result["origins"]["napkin"] == "env-only"
        (note,) = [n for n in result["notes"] if "napkin" in n]
        assert "no declaration" in note
        assert "LMER_NAPKIN_REPO" in note
        assert any(
            b == "source napkin: https://git.example.com/org/other-napkin.git (env-only)"
            for b in result["banners"]
        )
        assert all(c["name"] != "napkin" for c in result["clones"])

    def test_neither_is_silent_legacy(self):
        """Row 5: no declaration, no env — literally zero output."""
        result = _matrix(_cfg(), {})
        assert result["exit_code"] == 0
        assert result["notes"] == []
        assert result["banners"] == []
        assert result["errors"] == []
        assert result["env_updates"] == {}
        assert result["clones"] == []
        assert result["origins"] == {
            "taskdef": "unset-fallback",
            "napkin": "unset-fallback",
        }


class TestMatrixRefRows:
    """The per-field matrix for the taskdef ref (spec: ref-only mismatch is
    still a mismatch)."""

    def test_declared_ref_only_propagates(self):
        env = {}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "stable"}), env)
        assert result["env_updates"]["LMER_TASKDEF_REF"] == "stable"
        (clone,) = result["clones"]
        assert clone["ref"] == "stable"

    def test_equal_ref_env_wins_silently(self):
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL, "LMER_TASKDEF_REF": "stable"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "stable"}), env)
        assert "LMER_TASKDEF_REF" not in result["env_updates"]
        assert result["notes"] == []
        (clone,) = result["clones"]
        assert clone["ref"] == "stable"

    def test_ref_only_mismatch_headless_exits_2(self):
        """Repo agrees (env-match) but the ref differs → still exit 2."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL, "LMER_TASKDEF_REF": "dev"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "stable"}), env, isatty=False
        )
        assert result["exit_code"] == 2
        (error,) = result["errors"]
        assert "ref" in error
        assert "stable" in error and "dev" in error
        assert "LMER_TASKDEF_REF" in error
        assert result["clones"] == []

    def test_ref_mismatch_tty_pick_declared(self):
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL, "LMER_TASKDEF_REF": "dev"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "stable"}),
            env,
            isatty=True,
            input_fn=_scripted_input(["d"]),
        )
        assert result["env_updates"]["LMER_TASKDEF_REF"] == "stable"
        (clone,) = result["clones"]
        assert clone["ref"] == "stable"

    def test_ref_mismatch_tty_pick_env(self):
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL, "LMER_TASKDEF_REF": "dev"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "stable"}),
            env,
            isatty=True,
            input_fn=_scripted_input(["e"]),
        )
        assert "LMER_TASKDEF_REF" not in result["env_updates"]
        (clone,) = result["clones"]
        assert clone["ref"] == "dev"

    def test_env_only_ref_notes_missing_declaration(self):
        """Repo declared, ref env-only → env ref used with a one-line note."""
        env = {"LMER_TASKDEF_REF": "dev"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env)
        assert "LMER_TASKDEF_REF" not in result["env_updates"]
        (note,) = result["notes"]
        assert "no ref declared" in note
        (clone,) = result["clones"]
        assert clone["ref"] == "dev"


class TestPromptLayer:
    def test_source_choice_reprompts_until_valid(self, capsys):
        choice = clone_and_exec._prompt_source_choice(
            "taskdef",
            "repo",
            DECLARED_TASKDEF,
            "https://git.example.com/org/other.git",
            "LMER_TASKDEF_REPO",
            input_fn=_scripted_input(["what", "", "d"]),
        )
        assert choice == "declared"
        err = capsys.readouterr().err
        assert DECLARED_TASKDEF in err
        assert "LMER_TASKDEF_REPO" in err

    def test_source_choice_env(self):
        choice = clone_and_exec._prompt_source_choice(
            "napkin", "repo", "a", "b", "LMER_NAPKIN_REPO",
            input_fn=_scripted_input(["e"]),
        )
        assert choice == "env"

    def test_continue_legacy_yes(self):
        assert clone_and_exec._prompt_continue_legacy(
            "taskdef", input_fn=_scripted_input(["y"])
        )

    def test_continue_legacy_defaults_to_abort(self):
        assert not clone_and_exec._prompt_continue_legacy(
            "taskdef", input_fn=_scripted_input([""])
        )

    def test_continue_legacy_reprompts_until_valid(self):
        assert not clone_and_exec._prompt_continue_legacy(
            "napkin", input_fn=_scripted_input(["maybe", "n"])
        )


class TestPromptsOnClosedStdin:
    """Iteration-4 review finding: interactivity is decided by
    sys.stdin.isatty() while the host allocates -it from ITS own stdin, so a
    session can be classified interactive and still hit EOF. Both prompt
    loops used to let EOFError escape as a traceback out of
    _resolve_declared_sources; they now take the documented safe path.
    """

    @staticmethod
    def _eof_input(prompt=""):
        raise EOFError

    def test_source_choice_reports_abort(self, capsys):
        choice = clone_and_exec._prompt_source_choice(
            "taskdef", "repo", DECLARED_TASKDEF, ENV_TASKDEF_OTHER,
            "LMER_TASKDEF_REPO", input_fn=self._eof_input,
        )
        assert choice == clone_and_exec.PROMPT_ABORTED

    def test_continue_legacy_aborts(self):
        # "Defaults to abort" already covered the unanswered case; EOF is one.
        assert not clone_and_exec._prompt_continue_legacy(
            "taskdef", input_fn=self._eof_input
        )

    def test_matrix_refuses_start_instead_of_raising(self, capsys):
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            isatty=True,
            input_fn=self._eof_input,
        )
        assert result["exit_code"] == 2
        assert result["clones"] == []
        (error,) = result["errors"]
        assert "stdin reached EOF" in error
        assert "repo mismatch" in error
        assert TOKEN not in error
        # Review nit: the operator DID launch interactively — advising it
        # again is advice they already took. The other two fixes stand.
        assert "launch interactively" not in error
        assert "update sources.yaml" in error
        assert "unset LMER_TASKDEF_REPO" in error

    def test_a_field_answered_before_the_eof_is_not_called_unanswered(self):
        # Review nit: with two conflicting fields, an EOF on the second used
        # to report the first — which the operator answered — with the same
        # "no answer was possible" clause.
        env = {
            "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER,
            "LMER_TASKDEF_REF": "env-ref",
        }
        answers = iter(["d"])

        def _one_then_eof(prompt=""):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "declared-ref"}),
            env,
            isatty=True,
            input_fn=_one_then_eof,
        )
        assert result["exit_code"] == 2
        errors = {e.split(":")[1].split()[0]: e for e in result["errors"]}
        assert "No answer was possible" not in errors["repo"]
        assert "answer for this field was discarded" in errors["repo"]
        assert "No answer was possible" in errors["ref"]

    def test_interactive_answer_still_wins_when_stdin_is_open(self):
        # The EOF path must not have swallowed the ordinary prompt row.
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            isatty=True,
            input_fn=_scripted_input(["d"]),
        )
        assert result["exit_code"] == 0
        assert result["origins"]["taskdef"] == "declared"


class TestApplySourcesResolution:
    def test_success_propagates_env_and_clones_loudly(self, capsys):
        env = {"LMER_WORK_REPO_PATH": "/work"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "main"}), env)
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=False, input_fn=_no_input
            )
        assert rc == 0
        mock_clone.assert_called_once_with(
            Path("/taskdef"), ENV_TASKDEF_EQUAL, None, "main",
            manage_existing=True,
        )
        # The clone gets the resolved credentialed transport input, while the
        # environment retained for the agent keeps only the clean URL (#310).
        assert env["LMER_TASKDEF_REPO"] == DECLARED_TASKDEF
        assert env["LMER_TASKDEF_REF"] == "main"
        assert env["LMER_TASKDEF_PATHS"] == "/taskdef"
        err = capsys.readouterr().err
        assert f"source taskdef: {DECLARED_TASKDEF} (declared)" in err
        assert TOKEN not in err

    def test_headless_declared_clone_failure_refuses_start(self, capsys):
        """Spec N3: never the aux-clone warn-and-continue — headless exits 2
        with scrubbed remediation."""
        env = {"LMER_WORK_REPO_PATH": "/work"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF}), env)
        failure = subprocess.CalledProcessError(
            128, ["git", "clone", ENV_TASKDEF_EQUAL, "/taskdef"]
        )
        with patch.object(clone_and_exec, "ensure_clone", side_effect=failure):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        err = capsys.readouterr().err
        assert "declared source taskdef clone failed" in err
        # The remediation names BOTH fixes: the credential-scope
        # requirement (the work-repo credential needs read access)...
        assert "work-repo credential" in err and "can read it" in err
        # ...and the env-var override.
        assert "LMER_TASKDEF_REPO" in err
        assert TOKEN not in err

    def test_interactive_continue_legacy_reverts_propagation(self, capsys):
        """Continue-legacy reverts that source's mutations: no dangling env
        var, LMER_TASKDEF_PATHS restored without /taskdef."""
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_PATHS": "/mnt/td0"}
        result = _matrix(_cfg(taskdef={"repo": DECLARED_TASKDEF, "ref": "main"}), env)
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("boom")):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=True, input_fn=_scripted_input(["y"])
            )
        assert rc == 0
        assert "LMER_TASKDEF_REPO" not in env
        assert "LMER_TASKDEF_REF" not in env
        assert env["LMER_TASKDEF_PATHS"] == "/mnt/td0"
        assert "legacy mode" in capsys.readouterr().err

    def test_interactive_continue_legacy_reverts_napkin_separate_mode(self):
        """Napkin revert: LMER_NAPKIN_PATH back to the work-repo subdir so
        nothing links ~/napkin at a clone that does not exist."""
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_NAPKIN_PATH": "/work/napkin"}
        result = _matrix(_cfg(napkin={"repo": DECLARED_NAPKIN}), env)
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("boom")):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=True, input_fn=_scripted_input(["y"])
            )
        assert rc == 0
        assert "LMER_NAPKIN_REPO" not in env
        assert env["LMER_NAPKIN_PATH"] == "/work/napkin"

    def test_interactive_abort_returns_2(self, capsys):
        env = {"LMER_WORK_REPO_PATH": "/work"}
        result = _matrix(_cfg(napkin={"repo": DECLARED_NAPKIN}), env)
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("boom")):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=True, input_fn=_scripted_input(["n"])
            )
        assert rc == 2
        assert "aborting" in capsys.readouterr().err

    def test_anonymous_derivation_failure_names_the_anonymous_attempt(self, capsys):
        """DERIVED_ANONYMOUS marks the loud-failure path: the message says
        the clone was attempted anonymously (credential-less work repo)."""
        env = {"LMER_WORK_REPO_PATH": "/work"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}), env, work_url=WORK_URL_ANON
        )
        (clone,) = result["clones"]
        assert clone["derived_mode"] == sources_mod.DERIVED_ANONYMOUS
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("403")):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        assert "anonymously" in capsys.readouterr().err

    def test_withheld_credential_failure_names_the_port(self, capsys):
        """The second anonymous mode: the work repo DOES carry a credential
        and it was withheld because the declaration names another port. The
        hint must say that rather than "carries no credential", which would
        send the operator to look at the wrong thing.
        """
        env = {"LMER_WORK_REPO_PATH": "/work"}
        other_port = "https://git.example.com:9999/org/taskdefs.git"
        result = _matrix(_cfg(taskdef={"repo": other_port}), env, work_url=WORK_URL)
        (clone,) = result["clones"]
        assert clone["derived_mode"] == sources_mod.DERIVED_ANONYMOUS_OTHER_PORT
        assert clone["url"] == other_port
        assert TOKEN not in clone["url"]
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("403")):
            rc = clone_and_exec._apply_sources_resolution(
                result, env, sources_mod, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        err = capsys.readouterr().err
        assert "different port" in err and "withheld" in err
        assert "carries no credential" not in err
        assert TOKEN not in err


class TestSourcesModuleUnloadable:
    """Iteration-4 review finding: exec_module failures escaped as a raw
    traceback AFTER the work-repo clone, instead of the refuse-start path
    the sibling PyYAML guard provides for the same failure class (a present
    sources.yaml that cannot be read). The realistic trigger is a dev
    session live-mounting a partial src/ tree over the baked image.
    """

    def test_loader_failure_refuses_start_with_a_message(self, tmp_path, capsys):
        (tmp_path / "sources.yaml").write_text(
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n"
        )
        boom = ModuleNotFoundError("No module named 'sources'")
        with _with_yaml_available(), patch.object(
            clone_and_exec, "_load_sources_module", side_effect=boom
        ):
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env={}, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        err = capsys.readouterr().err
        assert "sources.yaml" in err
        assert "could not be loaded" in err
        assert "ModuleNotFoundError" in err
        # Refusal, not a traceback.
        assert "Traceback" not in err

    def test_no_sources_yaml_never_reaches_the_loader(self, tmp_path):
        # Silent legacy mode must stay stdlib-only: a broken resolver is
        # irrelevant when there is nothing to resolve.
        with patch.object(
            clone_and_exec, "_load_sources_module", side_effect=AssertionError("loaded")
        ):
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env={}, isatty=False, input_fn=_no_input
            )
        assert rc == 0


class TestEnvMatchRowAcrossACustomSshPort:
    """Iteration-5 followup (sixth finding, reproduced): with the work repo
    on ssh://…:2222 and the declaration in clean https form, the env var
    holding the working ssh form landed on the MISMATCH row — prompt
    interactively, exit 2 headless — even though it is byte-identical to
    what derive_clone_url produces from the declaration. LMER-CLI.md
    documents keeping that env var set as the way to keep a declared source
    clone-cache warm, so following the docs broke headless startup.
    """

    WORK_SSH_2222 = "ssh://git@git.example.com:2222/org/work.git"
    ENV_SSH_2222 = "ssh://git@git.example.com:2222/org/taskdefs.git"

    def test_derived_env_value_is_env_match_not_a_conflict(self):
        env = {"LMER_TASKDEF_REPO": self.ENV_SSH_2222}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            work_url=self.WORK_SSH_2222,
        )
        assert result["exit_code"] == 0
        assert result["origins"]["taskdef"] == "env-match"
        # Banners are scrubbed, and the redactor drops bare ssh userinfo too.
        assert result["banners"] == [
            "source taskdef: ssh://git.example.com:2222/org/taskdefs.git (env-match)"
        ]

    def test_a_different_repo_on_the_same_port_still_conflicts(self):
        env = {"LMER_TASKDEF_REPO": "ssh://git@git.example.com:2222/org/other.git"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            work_url=self.WORK_SSH_2222,
        )
        assert result["exit_code"] == 2

    def test_a_different_port_still_conflicts(self):
        # Only the port the derivation itself produces is folded — the
        # normalizer keeps ports everywhere else on purpose.
        env = {"LMER_TASKDEF_REPO": "https://git.example.com:9999/org/taskdefs.git"}
        result = _matrix(
            _cfg(taskdef={"repo": DECLARED_TASKDEF}),
            env,
            work_url=self.WORK_SSH_2222,
        )
        assert result["exit_code"] == 2


class TestResolveDeclaredSources:
    """The orchestrator seam main() calls (file → load → matrix → apply)."""

    def test_absent_file_is_zero_output_even_with_env_set(self, tmp_path, capsys):
        """Spec G5: no sources.yaml → the matrix never engages. Zero output,
        env untouched, exit 0 — behavior identical to today."""
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        rc = clone_and_exec._resolve_declared_sources(
            tmp_path, WORK_URL, env=env, isatty=False, input_fn=_no_input
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert env == {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}

    def test_declared_only_end_to_end(self, tmp_path, capsys):
        (tmp_path / "sources.yaml").write_text(
            "schema: 1\n"
            "sources:\n"
            "  taskdef:\n"
            f"    repo: {DECLARED_TASKDEF}\n"
            "    ref: main\n"
            "  napkin:\n"
            f"    repo: {DECLARED_NAPKIN}\n"
        )
        env = {"LMER_WORK_REPO_PATH": "/work"}
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=False, input_fn=_no_input
            )
        assert rc == 0
        assert env["LMER_TASKDEF_REPO"] == DECLARED_TASKDEF
        assert env["LMER_TASKDEF_REF"] == "main"
        assert env["LMER_TASKDEF_PATHS"] == "/taskdef"
        assert env["LMER_NAPKIN_REPO"] == DECLARED_NAPKIN
        assert env["LMER_NAPKIN_PATH"] == "/napkin"
        assert mock_clone.call_count == 2
        err = capsys.readouterr().err
        assert f"source taskdef: {DECLARED_TASKDEF} (declared)" in err
        assert f"source napkin: {DECLARED_NAPKIN} (declared)" in err
        assert TOKEN not in err

    def test_headless_mismatch_exits_2_before_any_clone(self, tmp_path, capsys):
        (tmp_path / "sources.yaml").write_text(
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n"
        )
        env = {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        mock_clone.assert_not_called()
        err = capsys.readouterr().err
        assert "mismatch" in err
        assert TOKEN not in err
        # No propagation happened.
        assert env == {"LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}

    def test_untrustable_config_refuses_start_scrubbed(self, tmp_path, capsys):
        """A cross-host declaration is a refuse-start error naming the env
        override — and the message never carries the work-repo credential."""
        (tmp_path / "sources.yaml").write_text(
            "schema: 1\nsources:\n  taskdef:\n    repo: https://evil.example.net/org/x.git\n"
        )
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env={}, isatty=False, input_fn=_no_input
            )
        assert rc == 2
        mock_clone.assert_not_called()
        err = capsys.readouterr().err
        assert "LMER_TASKDEF_REPO" in err
        assert TOKEN not in err

    def test_warnings_are_printed_but_do_not_block(self, tmp_path, capsys):
        (tmp_path / "sources.yaml").write_text(
            "schema: 1\nfuture_key: 1\nsources: {}\n"
        )
        rc = clone_and_exec._resolve_declared_sources(
            tmp_path, WORK_URL, env={}, isatty=False, input_fn=_no_input
        )
        assert rc == 0
        assert "future_key" in capsys.readouterr().err


class TestEnvDeltasPerRow:
    """Exact env deltas per matrix row (spec N1, plan task 7).

    Resolution must feed the derived values exactly as if the operator had
    set the env vars at launch: each test runs the full seam
    (_resolve_declared_sources: file → load → matrix → apply) against a
    snapshot env dict and asserts the ENTIRE post-resolution env by
    equality — proving both the mutations that must land and, just as
    load-bearing, that nothing else moved (env-only and legacy rows stay
    byte-identical to today; separate-napkin mode is derived downstream
    from LMER_NAPKIN_REPO, never a new env var)."""

    def _resolve(self, tmp_path, yaml_text, env, isatty=False, input_fn=_no_input,
                 clone=None):
        (tmp_path / "sources.yaml").write_text(yaml_text)
        with patch.object(
            clone_and_exec, "ensure_clone", side_effect=clone
        ) as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=isatty, input_fn=input_fn
            )
        return rc, mock_clone

    def test_declared_only_taskdef_exact_delta(self, tmp_path):
        """Declared-only taskdef: exactly REPO + REF + the /taskdef PATHS
        append (replicating the cli.py host-side conditional) — repo tier
        LAST so hooks/start.py taskdef_search_dirs keeps its slot order."""
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_PATHS": "/mnt/td0"}
        env = dict(pre)
        rc, _ = self._resolve(
            tmp_path,
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n    ref: main\n",
            env,
        )
        assert rc == 0
        assert env == {
            **pre,
            "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
            "LMER_TASKDEF_REF": "main",
            "LMER_TASKDEF_PATHS": "/mnt/td0:/taskdef",
        }
        # Repo tier LAST: external mounts keep precedence over /taskdef.
        assert env["LMER_TASKDEF_PATHS"].split(":")[-1] == "/taskdef"

    def test_declared_only_taskdef_no_prior_paths_exact_delta(self, tmp_path):
        """No pre-existing LMER_TASKDEF_PATHS → the append creates it as
        exactly '/taskdef' (what cli.py injects for a repo-only launch)."""
        pre = {"LMER_WORK_REPO_PATH": "/work"}
        env = dict(pre)
        rc, _ = self._resolve(
            tmp_path,
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n",
            env,
        )
        assert rc == 0
        assert env == {
            **pre,
            "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
            "LMER_TASKDEF_PATHS": "/taskdef",
        }

    def test_declared_only_napkin_exact_delta(self, tmp_path):
        """Declared-only napkin: exactly REPO + the LMER_NAPKIN_PATH
        recompute to /napkin (mirroring cli.py _resolve_napkin_path).
        Full-dict equality proves separate mode introduces NO new env var —
        main() derives napkin_is_separate as bool(LMER_NAPKIN_REPO)."""
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_NAPKIN_PATH": "/work/napkin"}
        env = dict(pre)
        rc, _ = self._resolve(
            tmp_path,
            f"schema: 1\nsources:\n  napkin:\n    repo: {DECLARED_NAPKIN}\n",
            env,
        )
        assert rc == 0
        assert env == {
            **pre,
            "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
            "LMER_NAPKIN_PATH": "/napkin",
        }

    def test_env_override_row_env_byte_identical(self, tmp_path):
        """Env-override (normalized-equal, row 2): the host already set and
        derived everything at launch — resolution must leave the env
        byte-identical while still cloning the env (credentialed) URL."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL,
            "LMER_TASKDEF_PATHS": "/mnt/td0:/taskdef",
        }
        env = dict(pre)
        rc, mock_clone = self._resolve(
            tmp_path,
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n",
            env,
        )
        assert rc == 0
        assert env == pre
        mock_clone.assert_called_once_with(
            Path("/taskdef"), ENV_TASKDEF_EQUAL, None, None,
            manage_existing=True,
        )

    def test_env_only_row_env_byte_identical(self, tmp_path, capsys):
        """Env-only (row 4): no declaration for the source — env stays
        byte-identical (note + banner only, clone left to clone_aux_repos)."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        env = dict(pre)
        rc, mock_clone = self._resolve(tmp_path, "schema: 1\nsources: {}\n", env)
        assert rc == 0
        assert env == pre
        mock_clone.assert_not_called()
        assert "env-only" in capsys.readouterr().err

    def test_legacy_row_env_byte_identical_and_silent(self, tmp_path, capsys):
        """Legacy (row 5): source neither declared nor in the env — env
        byte-identical and zero output for it (sources.yaml present but
        empty engages the matrix without producing anything)."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_NAPKIN_PATH": "/work/napkin",
            "LMER_TASKDEF_PATHS": "/mnt/td0",
        }
        env = dict(pre)
        rc, mock_clone = self._resolve(tmp_path, "schema: 1\nsources: {}\n", env)
        assert rc == 0
        assert env == pre
        mock_clone.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_mismatch_headless_env_untouched_before_exit(self, tmp_path):
        """Headless mismatch (row 3): exit 2 with the env byte-identical —
        including the OTHER source's would-be propagation (napkin here is
        declared-only and clean, but nothing may be applied on refusal)."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER,
            "LMER_TASKDEF_PATHS": "/mnt/td0:/taskdef",
            "LMER_NAPKIN_PATH": "/work/napkin",
        }
        env = dict(pre)
        rc, mock_clone = self._resolve(
            tmp_path,
            "schema: 1\nsources:\n"
            f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"
            f"  napkin:\n    repo: {DECLARED_NAPKIN}\n",
            env,
            isatty=False,
        )
        assert rc == 2
        assert env == pre
        mock_clone.assert_not_called()

    def test_continue_legacy_reverts_only_that_source_exact_env(self, tmp_path):
        """Continue-legacy after a declared taskdef clone failure: the
        taskdef mutations are reverted to the EXACT pre-resolution state
        (per-source legacy_env, not a global rollback) while the napkin
        source's propagation — resolved in the same pass — is kept."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_PATHS": "/mnt/td0",
            "LMER_NAPKIN_PATH": "/work/napkin",
        }
        env = dict(pre)

        def clone(dest, url, branch, ref, *, manage_existing):
            assert manage_existing is True
            if Path(dest) == Path("/taskdef"):
                raise Exception("boom")

        rc, mock_clone = self._resolve(
            tmp_path,
            "schema: 1\nsources:\n"
            f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n    ref: main\n"
            f"  napkin:\n    repo: {DECLARED_NAPKIN}\n",
            env,
            isatty=True,
            input_fn=_scripted_input(["y"]),
            clone=clone,
        )
        assert rc == 0
        assert mock_clone.call_count == 2  # both declared clones attempted
        assert env == {
            # taskdef: exact pre-resolution state restored (no REPO/REF,
            # PATHS without /taskdef)...
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_PATHS": "/mnt/td0",
            # ...napkin: propagation kept.
            "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
            "LMER_NAPKIN_PATH": "/napkin",
        }

    def test_continue_legacy_second_failure_keeps_first_source_intact(self, tmp_path):
        """Mirror ordering of the previous test: BOTH sources declared, the
        FIRST clone (taskdef — SOURCE_ENV_OVERRIDES order) succeeds and the
        SECOND (napkin) fails with continue-legacy. The napkin revert is
        strictly per-source: taskdef's already-applied propagation must
        survive byte-identical and its completed clone stay in place
        (exactly one /taskdef ensure_clone call, nothing undoing it)."""
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_NAPKIN_PATH": "/work/napkin"}
        env = dict(pre)
        clone_calls = []

        def clone(dest, url, branch, ref, *, manage_existing):
            assert manage_existing is True
            clone_calls.append((Path(dest), url, ref))
            if Path(dest) == Path("/napkin"):
                raise Exception("boom")

        rc, mock_clone = self._resolve(
            tmp_path,
            "schema: 1\nsources:\n"
            f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n    ref: main\n"
            f"  napkin:\n    repo: {DECLARED_NAPKIN}\n",
            env,
            isatty=True,
            input_fn=_scripted_input(["y"]),
            clone=clone,
        )
        assert rc == 0
        # The taskdef clone happened first, succeeded, and was never retried
        # or removed — a valid clone remains for the /taskdef search path.
        assert clone_calls == [
            (Path("/taskdef"), ENV_TASKDEF_EQUAL, "main"),
            (Path("/napkin"), f"https://oauth2:{TOKEN}@git.example.com/org/napkin.git", None),
        ]
        assert env == {
            # taskdef: propagation kept in full (REPO + REF + PATHS append)...
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
            "LMER_TASKDEF_REF": "main",
            "LMER_TASKDEF_PATHS": "/taskdef",
            # ...napkin: exact pre-resolution state restored (no REPO, PATH
            # back at the work-repo subdir — no dangling /napkin pointer).
            "LMER_NAPKIN_PATH": "/work/napkin",
        }


class TestFailureSignalSeam:
    """The failure-signal split at the resolution/aux seam: declared URLs
    fail LOUDLY on the resolving side (exit 2 / prompt); env-sourced
    (env-only, undeclared) and absent URLs keep clone_aux_repos'
    warn-and-continue contract exactly (see the clone_aux_repos docstring)."""

    def _resolve(self, tmp_path, yaml_text, env, isatty=False, input_fn=_no_input,
                 clone=None):
        (tmp_path / "sources.yaml").write_text(yaml_text)
        with patch.object(
            clone_and_exec, "ensure_clone", side_effect=clone
        ) as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=isatty, input_fn=input_fn
            )
        return rc, mock_clone

    def test_env_only_url_failure_stays_aux_warn_and_continue(self, tmp_path, capsys):
        """An env-only URL is never cloned by resolution — even headless,
        where a declared failure would exit 2. Its clone happens downstream
        in clone_aux_repos, where failure keeps today's behavior EXACTLY:
        non-fatal, one scrubbed '(continuing)' warn line, no loud-path
        wording, env untouched."""
        env = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        pre = dict(env)
        failure = subprocess.CalledProcessError(
            128, ["git", "clone", ENV_NAPKIN_OTHER, "/napkin"]
        )
        rc, mock_clone = self._resolve(
            tmp_path, "schema: 1\nsources: {}\n", env, isatty=False, clone=failure
        )
        assert rc == 0
        mock_clone.assert_not_called()  # resolution planned no loud clone
        with patch.object(clone_and_exec, "ensure_clone", side_effect=failure):
            # Must not raise and must not signal failure to the caller.
            assert clone_and_exec.clone_aux_repos(env["LMER_NAPKIN_REPO"], None, None) is None
        assert env == pre
        err = capsys.readouterr().err
        assert "napkin clone failed (continuing)" in err
        assert "declared source" not in err  # never the loud path
        assert "Refusing to start" not in err
        assert TOKEN not in err

    def test_interactive_abort_exits_2_and_reentry_is_idempotent(self, tmp_path, capsys):
        """Abort (the continue-legacy prompt's default) exits 2 leaving
        nothing a next run can see: env mutations die with the process
        (every run re-derives from the host env) and the failed clone left
        no .git behind for ensure_clone to no-op on. Re-entry from the same
        host env replays the identical prompt-and-abort — and succeeds
        cleanly once the source is reachable."""
        yaml_text = f"schema: 1\nsources:\n  napkin:\n    repo: {DECLARED_NAPKIN}\n"
        napkin_cred = f"https://oauth2:{TOKEN}@git.example.com/org/napkin.git"
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_NAPKIN_PATH": "/work/napkin"}

        # Run 1: declared clone fails, user aborts → refuse start.
        rc1, _ = self._resolve(
            tmp_path, yaml_text, dict(pre), isatty=True,
            input_fn=_scripted_input(["n"]), clone=Exception("boom"),
        )
        assert rc1 == 2
        err1 = capsys.readouterr().err
        assert "aborting" in err1
        # The abort remediation names both fixes too (credential scope +
        # env-var override), matching the headless message.
        assert "work-repo credential" in err1 and "can read it" in err1
        assert "LMER_NAPKIN_REPO" in err1
        assert TOKEN not in err1

        # Run 2 (re-entry, fresh env snapshot from the same host state):
        # byte-identical behavior — same prompt, same abort, same exit.
        rc2, _ = self._resolve(
            tmp_path, yaml_text, dict(pre), isatty=True,
            input_fn=_scripted_input(["n"]), clone=Exception("boom"),
        )
        assert rc2 == 2
        assert capsys.readouterr().err == err1

        # Run 3: the source is reachable now → clean success with the full
        # declared propagation, no residue from the aborted runs.
        env = dict(pre)
        rc3, mock_clone = self._resolve(
            tmp_path, yaml_text, env, isatty=True, input_fn=_no_input, clone=None
        )
        assert rc3 == 0
        mock_clone.assert_called_once_with(
            Path("/napkin"), napkin_cred, None, None,
            manage_existing=True,
        )
        assert env == {
            **pre,
            "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
            "LMER_NAPKIN_PATH": "/napkin",
        }


class TestMainSeamWiring:
    def test_main_headless_mismatch_exits_2_before_aux_clones(
        self, tmp_path, monkeypatch, capsys
    ):
        """Integration: main() refuses start (exit 2) on a headless mismatch
        BEFORE clone_aux_repos and before any /taskdef//napkin clone."""
        work = tmp_path / "work"

        def fake_ensure_clone(workspace, repo_url, branch, ref, **kwargs):
            assert Path(workspace) not in (Path("/taskdef"), Path("/napkin")), (
                "no aux clone may run before resolution refuses start"
            )
            Path(workspace).mkdir(parents=True, exist_ok=True)
            if Path(workspace) == work:
                (work / "sources.yaml").write_text(
                    f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n"
                )

        for var in (
            "LMER_SERVICE_MODE", "LMER_REPO_URL", "LMER_CHECKOUT_BRANCH",
            "LMER_CHECKOUT_REF", "GITLAB_MR_ID", "LMER_SECONDARY_TARGETS",
            "LMER_NAPKIN_REPO", "LMER_TASKDEF_REF", "LMER_TASKDEF_PATHS",
            "LMER_TRUST_MISE",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LMER_NO_REPO", "1")
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_TASKDEF_REPO", ENV_TASKDEF_OTHER)
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # headless
        aux = MagicMock()
        monkeypatch.setattr(clone_and_exec, "ensure_clone", fake_ensure_clone)
        monkeypatch.setattr(clone_and_exec, "clone_aux_repos", aux)
        # Paranoia: dispatch must never be reached in this test.
        monkeypatch.setattr(
            clone_and_exec.os, "execv",
            MagicMock(side_effect=AssertionError("dispatch reached")),
        )

        rc = clone_and_exec.main(["--", "true"])

        assert rc == 2
        aux.assert_not_called()
        err = capsys.readouterr().err
        assert "mismatch" in err
        assert "git.example.com/org/taskdefs.git" in err
        assert "git.example.com/org/other.git" in err
        assert TOKEN not in err

    def test_main_propagation_lands_in_os_environ_before_downstream_reads(
        self, tmp_path, monkeypatch
    ):
        """Integration (spec N1 ordering): resolution mutates os.environ
        BEFORE clone_aux_repos re-reads LMER_NAPKIN_REPO / LMER_TASKDEF_REPO /
        LMER_TASKDEF_REF and before the napkin_path / napkin_is_separate
        reads — clone_aux_repos receives the resolved URLs (and sees them in
        os.environ at call time), setup_napkin_and_links gets
        napkin_path=/napkin with napkin_is_separate=True derived from
        bool(LMER_NAPKIN_REPO), and the total os.environ delta is exactly
        the five propagated source vars plus the cleaned work URL and
        non-secret credential-file path (no separate-mode env var)."""
        work = tmp_path / "work"
        credential_file = tmp_path / "work-credential"
        credential_file.write_text(WORK_URL + "\n")

        def fake_ensure_clone(workspace, repo_url, branch, ref, **kwargs):
            # Only materialize the work repo (the /taskdef and /napkin
            # declared clones must not touch the real filesystem root).
            if Path(workspace) == work:
                work.mkdir(parents=True, exist_ok=True)
                (work / "sources.yaml").write_text(
                    "schema: 1\nsources:\n"
                    f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n    ref: main\n"
                    f"  napkin:\n    repo: {DECLARED_NAPKIN}\n"
                )
                return clone_and_exec._SessionGitCredential(
                    clean_url=WORK_URL_ANON,
                    scope_url=WORK_URL_ANON,
                    path=credential_file,
                )
            return None

        for var in (
            "LMER_SERVICE_MODE", "LMER_REPO_URL", "LMER_CHECKOUT_BRANCH",
            "LMER_CHECKOUT_REF", "GITLAB_MR_ID", "LMER_SECONDARY_TARGETS",
            "LMER_NAPKIN_REPO", "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF",
            "LMER_TASKDEF_PATHS", "LMER_TRUST_MISE", "LMER_REPO_HOST",
            "LMER_REPO_PROJECT", "LMER_TASK", "LMER_TASK_TARGET",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LMER_NO_REPO", "1")
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_NAPKIN_PATH", f"{work}/napkin")
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # headless

        env_at_aux_call = {}

        def capture_aux(napkin_repo_url, taskdef_repo_url, taskdef_ref):
            env_at_aux_call.update(
                {
                    k: os.environ.get(k)
                    for k in (
                        "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF",
                        "LMER_TASKDEF_PATHS", "LMER_NAPKIN_REPO",
                        "LMER_NAPKIN_PATH",
                    )
                }
            )

        aux = MagicMock(side_effect=capture_aux)
        links = MagicMock()
        monkeypatch.setattr(clone_and_exec, "ensure_clone", fake_ensure_clone)
        monkeypatch.setattr(clone_and_exec, "clone_aux_repos", aux)
        monkeypatch.setattr(clone_and_exec, "setup_napkin_and_links", links)
        monkeypatch.setattr(clone_and_exec, "ensure_work_repo_directory", MagicMock())
        monkeypatch.setattr(clone_and_exec, "provision_documentation", MagicMock())
        monkeypatch.setattr(clone_and_exec, "_trust_mise_config", MagicMock())
        monkeypatch.setattr(clone_and_exec.os, "execv", MagicMock())

        pre_env = dict(os.environ)
        with patch.dict(os.environ):  # restore resolution's direct mutations
            rc = clone_and_exec.main(["--", "true"])
            post_env = dict(os.environ)

        assert rc == 0
        # The total delta is exactly the propagated vars — as if the
        # operator had set them at launch, and nothing else (in particular
        # no separate-napkin-mode env var).
        delta = {k: v for k, v in post_env.items() if pre_env.get(k) != v}
        assert delta == {
            "LMER_WORK_REPO": WORK_URL_ANON,
            "LMER_WORK_REPO_CREDENTIAL_FILE": str(credential_file),
            "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
            "LMER_TASKDEF_REF": "main",
            "LMER_TASKDEF_PATHS": "/taskdef",
            "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        assert set(pre_env) - set(post_env) == set()
        # clone_aux_repos got the resolved values (read back from the env)...
        aux.assert_called_once_with(DECLARED_NAPKIN, DECLARED_TASKDEF, "main")
        # ...and os.environ already carried them at call time.
        assert env_at_aux_call == {
            key: value for key, value in delta.items()
            if key not in {
                "LMER_WORK_REPO", "LMER_WORK_REPO_CREDENTIAL_FILE"
            }
        }
        # setup_napkin_and_links: /napkin + separate mode derived from
        # bool(LMER_NAPKIN_REPO), never from a dedicated env var.
        links.assert_called_once_with(
            work.resolve(), Path("/napkin"),
            napkin_is_separate=True,
            home=Path(os.environ.get("HOME", "/home/developer")),
        )


class TestEndToEndAcceptance:
    """Plan task 9 — in-process end-to-end acceptance (no container).

    The remaining acceptance angles the seam/matrix/env-delta classes above
    do not reach: (1) the propagated /taskdef LMER_TASKDEF_PATHS entry lands
    in the hooks/start.py taskdef_search_dirs() slot and a taskdef present
    only there RENDERS from there; (2) declared-only napkin makes the REAL
    setup_napkin_and_links link ~/napkin at the separate /napkin clone;
    (4) explicit backward-compat: absent sources.yaml — and a present one
    with no source keys — is zero-output with every source env var
    byte-identical. Acceptance angle (3), headless mismatch refusing start
    before any aux clone with scrubbed values, is already covered by
    TestMainSeamWiring.test_main_headless_mismatch_exits_2_before_aux_clones.
    """

    def test_declared_only_taskdef_renders_via_start_hook_search_dirs(
        self, tmp_path, monkeypatch, capsys
    ):
        """Acceptance (1): declared-only taskdef → resolution propagates
        /taskdef into the LMER_TASKDEF_PATHS slot of hooks/start.py
        taskdef_search_dirs(), and a taskdef name present ONLY under that
        slot renders from there (through the canonical search, never a
        fast-path env var). The fixed /taskdef clone destination cannot be
        materialized in a test, so the propagated entry is pointed at a tmp
        stand-in — same slot, same order — before handing the env to the
        hook."""
        import hooks.start as start_hook
        from tests.conftest import strip_lmer_env

        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text(
            f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n"
        )
        # Stand-in for the /taskdef clone destination; the fake clone
        # materializes the declared repo's content here.
        stand_in = tmp_path / "taskdef-clone"
        task_name = "sources-e2e-only-task"

        def fake_clone(dest, url, branch, ref, *, manage_existing):
            assert manage_existing is True
            assert Path(dest) == Path("/taskdef")
            assert url == ENV_TASKDEF_EQUAL
            task_dir = stand_in / task_name
            task_dir.mkdir(parents=True)
            (task_dir / "instructions.txt").write_text(
                "RENDERED-FROM-TASKDEF-SLOT {{ taskdef_name }}"
            )

        env = {"LMER_WORK_REPO_PATH": str(work), "LMER_TASKDEF_PATHS": "/mnt/td0"}
        with patch.object(clone_and_exec, "ensure_clone", side_effect=fake_clone):
            rc = clone_and_exec._resolve_declared_sources(
                work, WORK_URL, env=env, isatty=False, input_fn=_no_input
            )
        assert rc == 0
        # Propagation appended /taskdef as the LAST LMER_TASKDEF_PATHS entry.
        paths = env["LMER_TASKDEF_PATHS"].split(":")
        assert paths == ["/mnt/td0", "/taskdef"]

        # Hand the propagated env to the hook, with the /taskdef entry
        # substituted by the stand-in clone (slot and order preserved).
        strip_lmer_env(monkeypatch)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_TASKDEF_REPO", env["LMER_TASKDEF_REPO"])
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS",
            ":".join(str(stand_in) if p == "/taskdef" else p for p in paths),
        )

        dirs = start_hook.taskdef_search_dirs()
        # Slot check: the work repo has no taskdef dirs, so the search order
        # is exactly [external mount, /taskdef slot, built-in].
        assert dirs[0] == Path("/mnt/td0")
        assert dirs[1] == stand_in
        assert dirs[-1] == start_hook.builtin_taskdef_root()
        # The taskdef exists ONLY under the /taskdef slot.
        for other in dirs:
            if other != stand_in:
                assert not (other / task_name).exists()

        resolved = start_hook.find_taskdef_file("instructions.txt", task_name)
        assert resolved == stand_in / task_name / "instructions.txt"
        rendered = start_hook.render_taskdef_template(resolved)
        assert rendered == f"RENDERED-FROM-TASKDEF-SLOT {task_name}"
        # The greppable render banner names the /taskdef slot as the source.
        assert f"taskdef source: {stand_in} (schema 1)" in capsys.readouterr().out

    def test_main_declared_only_napkin_links_home_napkin_to_separate_clone(
        self, tmp_path, monkeypatch
    ):
        """Acceptance (2): declared-only napkin through main() with the REAL
        setup_napkin_and_links and a tmp HOME — ~/napkin ends up a symlink
        at the separate /napkin clone, not the work-repo subdir the launch
        env pointed at."""
        work = tmp_path / "work"
        home = tmp_path / "home"
        home.mkdir()

        def fake_ensure_clone(workspace, repo_url, branch, ref, **kwargs):
            # Only materialize the work repo; the declared /napkin clone
            # must not touch the real filesystem root.
            if Path(workspace) == work:
                work.mkdir(parents=True, exist_ok=True)
                (work / "sources.yaml").write_text(
                    f"schema: 1\nsources:\n  napkin:\n    repo: {DECLARED_NAPKIN}\n"
                )

        for var in (
            "LMER_SERVICE_MODE", "LMER_REPO_URL", "LMER_CHECKOUT_BRANCH",
            "LMER_CHECKOUT_REF", "GITLAB_MR_ID", "LMER_SECONDARY_TARGETS",
            "LMER_NAPKIN_REPO", "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF",
            "LMER_TASKDEF_PATHS", "LMER_TRUST_MISE", "LMER_REPO_HOST",
            "LMER_REPO_PROJECT", "LMER_TASK", "LMER_TASK_TARGET",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LMER_NO_REPO", "1")
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_NAPKIN_PATH", f"{work}/napkin")  # subdir mode at launch
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # headless

        monkeypatch.setattr(clone_and_exec, "ensure_clone", fake_ensure_clone)
        monkeypatch.setattr(clone_and_exec, "clone_aux_repos", MagicMock())
        monkeypatch.setattr(clone_and_exec, "ensure_work_repo_directory", MagicMock())
        monkeypatch.setattr(clone_and_exec, "provision_documentation", MagicMock())
        monkeypatch.setattr(clone_and_exec, "_trust_mise_config", MagicMock())
        monkeypatch.setattr(clone_and_exec.os, "execv", MagicMock())

        with patch.dict(os.environ):  # restore resolution's direct mutations
            rc = clone_and_exec.main(["--", "true"])

        assert rc == 0
        napkin_link = home / "napkin"
        assert napkin_link.is_symlink()
        # Linked at the separate clone, NOT the work-repo subdir.
        assert os.readlink(napkin_link) == "/napkin"
        assert os.readlink(napkin_link) != str(work / "napkin")
        work_link = home / "work"
        assert work_link.is_symlink()
        assert Path(os.readlink(work_link)) == work.resolve()

    def test_no_sources_yaml_full_legacy_env_byte_identical_and_silent(
        self, tmp_path, capsys
    ):
        """Acceptance (4a): no sources.yaml → the resolution path emits ZERO
        new stdout/stderr lines and leaves ALL FIVE source env vars
        (LMER_TASKDEF_REPO/LMER_NAPKIN_REPO/LMER_TASKDEF_REF/
        LMER_TASKDEF_PATHS/LMER_NAPKIN_PATH) byte-identical — the fully
        env-configured legacy launch never engages the matrix."""
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER,
            "LMER_TASKDEF_REF": "dev",
            "LMER_TASKDEF_PATHS": "/mnt/td0:/taskdef",
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        env = dict(pre)
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=False, input_fn=_no_input
            )
        assert rc == 0
        assert env == pre
        mock_clone.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_sources_yaml_without_source_keys_env_byte_identical_and_silent(
        self, tmp_path, capsys
    ):
        """Acceptance (4b): a present sources.yaml whose source keys are
        absent (no `sources:` mapping at all) with a legacy launch env —
        zero output, all five source env vars byte-identical (the set ones
        untouched, the unset ones still unset)."""
        (tmp_path / "sources.yaml").write_text("schema: 1\n")
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REF": "dev",  # ref without repo: row 5 never reads it
            "LMER_TASKDEF_PATHS": "/mnt/td0",
            "LMER_NAPKIN_PATH": "/work/napkin",
        }
        env = dict(pre)
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            rc = clone_and_exec._resolve_declared_sources(
                tmp_path, WORK_URL, env=env, isatty=False, input_fn=_no_input
            )
        assert rc == 0
        assert env == pre
        assert "LMER_TASKDEF_REPO" not in env
        assert "LMER_NAPKIN_REPO" not in env
        mock_clone.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


# --- Manifest tier (issue #370) ----------------------------------------------
# The second declaration file. A present `_plexus.yaml` is probed with the
# same stdlib test the placement code uses, read through the core's ONE
# manifest parser (lmer_schema.manifest.load_manifest — a function-local,
# guarded import, never module scope) and its raw `sources:` handed to the
# standalone loader's manifest tier. Precedence is env > manifest > legacy.
# Visibility rule: once a manifest is present every declared row names its
# tier (the loader's declared_origin_label) and the file it came from; a tree
# without one keeps every byte of today's output and never imports the core.

MANIFEST_NAME = clone_and_exec.PLEXUS_MANIFEST_NAME
ENTRYPOINT_PATH = Path(clone_and_exec.__file__).resolve()
# The credentialed clone URL derive_clone_url produces for DECLARED_NAPKIN.
NAPKIN_CLONE_URL = f"https://oauth2:{TOKEN}@git.example.com/org/napkin.git"
# ONE taskdef+napkin declaration, as the legacy file's `sources:` and as the
# manifest's: the R7 fold moves exactly these bytes from one file to the other.
SOURCES_BLOCK = (
    "  taskdef:\n"
    f"    repo: {DECLARED_TASKDEF}\n"
    "    ref: main\n"
    "  napkin:\n"
    f"    repo: {DECLARED_NAPKIN}\n"
)
LEGACY_DECLARATION = "schema: 1\nsources:\n" + SOURCES_BLOCK
SOURCE_ENV_VARS = (
    "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF", "LMER_TASKDEF_PATHS",
    "LMER_NAPKIN_REPO", "LMER_NAPKIN_PATH",
)
# What that declaration propagates from a bare env, and what it clones —
# the same at every tier (TestEnvDeltasPerRow pins the legacy half).
DECLARED_DELTA = {
    "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
    "LMER_TASKDEF_REF": "main",
    "LMER_TASKDEF_PATHS": "/taskdef",
    "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
    "LMER_NAPKIN_PATH": "/napkin",
}
DECLARED_CLONES = [
    (Path("/taskdef"), ENV_TASKDEF_EQUAL, "main"),
    (Path("/napkin"), NAPKIN_CLONE_URL, None),
]


def _manifest(sources_block=None, *, schema="1", extra=""):
    """A `_plexus.yaml` document: `schema:` and `name:` (both required by the
    core), *extra* raw top-level lines, and a `sources:` section when given."""
    text = f"schema: {schema}\nname: example\n{extra}"
    if sources_block is not None:
        text += f"sources:\n{sources_block}"
    return text


def _write_tree(root, *, manifest=None, legacy=None):
    root.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (root / MANIFEST_NAME).write_text(manifest)
    if legacy is not None:
        (root / "sources.yaml").write_text(legacy)
    return root


def _recording_clone(calls, failing=None):
    """An ensure_clone stand-in recording ``(dest, url, ref)`` per declared
    clone, failing the one at *failing* when given."""

    def clone(dest, url, branch, ref, *, manage_existing):
        assert manage_existing is True
        calls.append((Path(dest), url, ref))
        if failing is not None and Path(dest) == Path(failing):
            raise Exception("boom")

    return clone


def _resolve(root, env, isatty=False, input_fn=_no_input, clone=None):
    with patch.object(clone_and_exec, "ensure_clone", side_effect=clone) as mock_clone:
        rc = clone_and_exec._resolve_declared_sources(
            root, WORK_URL, env=env, isatty=isatty, input_fn=input_fn
        )
    return rc, mock_clone


def _without_core():
    """Patch context making every ``lmer_schema`` import raise ImportError (a
    build shipping without the core). Each submodule is nulled too: ``from
    x.y import z`` consults ``sys.modules["x.y"]`` first, so nulling the
    package alone would leave the import working in a run that already
    imported it (tests/test_clone_and_exec_session_dir.py's helper)."""
    return patch.dict(
        sys.modules,
        {
            "lmer_schema": None,
            "lmer_schema.form": None,
            "lmer_schema.manifest": None,
            "lmer_schema.registry": None,
        },
    )


class TestManifestTierGuards:
    """The refuse-start family extended to the manifest tier: a present
    `_plexus.yaml` whose `sources:` cannot be read is never silently ignored
    (D20 — the same rule the PyYAML guard applies to a present sources.yaml),
    and a tree without one never consults the core."""

    def test_pyyaml_missing_with_manifest_refuses_naming_the_manifest(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        with _with_yaml_missing():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 2
        err = capsys.readouterr().err
        assert str(tmp_path / MANIFEST_NAME) in err
        assert "PyYAML" in err and "lmer build" in err
        # PyYAML is blamed, not the core (which imports PyYAML at module scope
        # and would otherwise take the blame); removing sources.yaml is no fix.
        assert "lmer_schema" not in err
        assert "remove sources.yaml" not in err

    def test_pyyaml_missing_with_both_files_names_both(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(), legacy="schema: 1\n")
        with _with_yaml_missing():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 2
        err = capsys.readouterr().err
        assert f"{tmp_path / MANIFEST_NAME} and {tmp_path / 'sources.yaml'} are present" in err

    def test_legacy_only_pyyaml_refusal_keeps_todays_bytes(self, tmp_path, capsys):
        (tmp_path / "sources.yaml").write_text("schema: 1\n")
        with _with_yaml_missing():
            rc = clone_and_exec._refuse_start_if_sources_unreadable(tmp_path)
        assert rc == 2
        assert capsys.readouterr().err == (
            f"❌ {tmp_path / 'sources.yaml'} is present but PyYAML is not "
            "importable in the container interpreter — refusing to start rather "
            "than silently ignoring the declared sources. Rebuild the image with "
            "a lmer version that guarantees PyYAML: lmer build. (Or remove "
            "sources.yaml from the work repo to run without declarations.)\n"
        )

    def test_pyyaml_missing_with_manifest_refuses_through_the_seam(self, tmp_path, capsys):
        """The seam refuses BEFORE the core import is attempted: with PyYAML
        gone the core would fail to import too, and the message must name
        the real cause."""
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        env = {}
        with _with_yaml_missing():
            rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 2
        mock_clone.assert_not_called()
        assert env == {}
        err = capsys.readouterr().err
        assert "PyYAML" in err
        assert "lmer_schema" not in err
        assert "Traceback" not in err

    def test_core_missing_with_manifest_refuses_start(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        env = {}
        with _without_core():
            rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 2
        mock_clone.assert_not_called()
        assert env == {}
        err = capsys.readouterr().err
        assert str(tmp_path / MANIFEST_NAME) in err
        assert "lmer_schema.manifest" in err
        assert "lmer build" in err
        # A refusal, never a fall back to the legacy file and never a traceback.
        assert "Traceback" not in err

    def test_core_missing_without_manifest_is_never_consulted(self, tmp_path, capsys):
        """A legacy tree resolves with the core unimportable: the import is
        gated on the manifest probe, exactly like the placement code's."""
        _write_tree(tmp_path, legacy=LEGACY_DECLARATION)
        env = {"LMER_WORK_REPO_PATH": "/work"}
        calls = []
        with _without_core():
            rc, _ = _resolve(tmp_path, env, clone=_recording_clone(calls))
        assert rc == 0
        assert env == {"LMER_WORK_REPO_PATH": "/work", **DECLARED_DELTA}
        assert calls == DECLARED_CLONES
        assert "lmer_schema" not in capsys.readouterr().err

    def test_resolver_unloadable_with_manifest_names_the_manifest(self, tmp_path, capsys):
        """The sibling-module loader failure (TestSourcesModuleUnloadable)
        names the declaration file actually present."""
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        boom = ModuleNotFoundError("No module named 'sources'")
        with patch.object(clone_and_exec, "_load_sources_module", side_effect=boom):
            rc, mock_clone = _resolve(tmp_path, {})
        assert rc == 2
        mock_clone.assert_not_called()
        err = capsys.readouterr().err
        assert f"{tmp_path / MANIFEST_NAME} is present" in err
        assert "could not be loaded" in err
        assert "Traceback" not in err


class TestManifestRefusals:
    """ManifestError (the core's taxonomy) and SourcesConfigError from the
    manifest tier: exit 2, credential-scrubbed, nothing cloned, env untouched.
    A present manifest this lmer cannot read is never demoted to legacy."""

    @pytest.mark.parametrize(
        "schema, needle",
        [("true", "integer `schema:`"), ("99", "upgrade lmer")],
        ids=["bool-trap", "newer-int"],
    )
    def test_unsupported_manifest_schema_exits_2(self, tmp_path, capsys, schema, needle):
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK, schema=schema))
        env = {"LMER_WORK_REPO_PATH": "/work"}
        rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 2
        mock_clone.assert_not_called()
        assert env == {"LMER_WORK_REPO_PATH": "/work"}
        err = capsys.readouterr().err
        assert needle in err
        assert MANIFEST_NAME in err
        assert "Traceback" not in err

    def test_unparseable_manifest_exits_2(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest="schema: 1\nname: example\nsources: [oops\n")
        rc, mock_clone = _resolve(tmp_path, {})
        assert rc == 2
        mock_clone.assert_not_called()
        assert "unparseable" in capsys.readouterr().err

    def test_dangling_manifest_symlink_is_present_and_refuses(self, tmp_path, capsys):
        """is_symlink() first: someone put a manifest there, so the tree is
        schema-shaped even though exists() follows the link and says no."""
        (tmp_path / MANIFEST_NAME).symlink_to(tmp_path / "no-such-manifest.yaml")
        rc, mock_clone = _resolve(tmp_path, {})
        assert rc == 2
        mock_clone.assert_not_called()
        assert "unreadable" in capsys.readouterr().err

    def test_same_host_violation_in_manifest_exits_2_naming_the_env_var(self, tmp_path, capsys):
        _write_tree(
            tmp_path,
            manifest=_manifest("  napkin:\n    repo: https://evil.example.net/org/napkin.git\n"),
        )
        env = {}
        rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 2
        mock_clone.assert_not_called()
        assert env == {}
        err = capsys.readouterr().err
        assert "LMER_NAPKIN_REPO" in err
        # The message names the manifest, not the (absent) legacy file.
        assert str(tmp_path / MANIFEST_NAME) in err
        assert "sources.yaml" not in err
        assert TOKEN not in err

    def test_credential_in_manifest_refuses_and_scrubs(self, tmp_path, capsys):
        _write_tree(
            tmp_path,
            manifest=_manifest(
                f"  taskdef:\n    repo: https://oauth2:{TOKEN}@git.example.com/org/taskdefs.git\n"
            ),
        )
        rc, mock_clone = _resolve(tmp_path, {})
        assert rc == 2
        mock_clone.assert_not_called()
        err = capsys.readouterr().err
        assert "embeds a credential" in err
        assert TOKEN not in err

    def test_an_outranked_legacy_file_is_still_validated(self, tmp_path, capsys):
        """Both tiers are validated before the merge (the loader's rule): a
        cross-host legacy entry the manifest overrides still refuses start."""
        _write_tree(
            tmp_path,
            manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"),
            legacy="schema: 1\nsources:\n  taskdef:\n    repo: https://evil.example.net/org/x.git\n",
        )
        rc, mock_clone = _resolve(tmp_path, {})
        assert rc == 2
        mock_clone.assert_not_called()
        assert str(tmp_path / "sources.yaml") in capsys.readouterr().err


class TestManifestTierResolution:
    """The seam end to end with a manifest in play."""

    def test_a_manifest_only_declaration_resolves_end_to_end(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        env = {"LMER_WORK_REPO_PATH": "/work"}
        calls = []
        rc, _ = _resolve(tmp_path, env, clone=_recording_clone(calls))
        assert rc == 0
        assert env == {"LMER_WORK_REPO_PATH": "/work", **DECLARED_DELTA}
        assert calls == DECLARED_CLONES
        err = capsys.readouterr().err
        # Every declared row names its tier, through the loader's one label.
        assert f"source taskdef: {DECLARED_TASKDEF} (declared (manifest))\n" in err
        assert f"source napkin: {DECLARED_NAPKIN} (declared (manifest))\n" in err
        assert TOKEN not in err
        # No legacy file, no warning: nothing names sources.yaml.
        assert "sources.yaml" not in err

    def test_the_container_hands_the_manifest_tier_to_the_loader(self, tmp_path):
        """g1 review MAJOR 1: bin/doctor and the lmer_schema comments say the
        container reads the manifest tier — this is the call that makes it
        true. The raw `sources:` dict and the manifest path reach the
        standalone loader as `manifest_sources=` / `manifest_path=`."""
        _write_tree(tmp_path, manifest=_manifest(SOURCES_BLOCK))
        standalone = clone_and_exec._load_sources_module()
        with patch.object(standalone, "load_sources", wraps=standalone.load_sources) as spy:
            rc, _ = _resolve(tmp_path, {"LMER_WORK_REPO_PATH": "/work"})
        assert rc == 0
        spy.assert_called_once()
        args, kwargs = spy.call_args
        assert args == (tmp_path / "sources.yaml", WORK_URL)
        assert kwargs == {
            "manifest_sources": {
                "taskdef": {"repo": DECLARED_TASKDEF, "ref": "main"},
                "napkin": {"repo": DECLARED_NAPKIN},
            },
            "manifest_path": tmp_path / MANIFEST_NAME,
        }

    def test_a_tree_without_a_manifest_calls_the_loader_legacy_only(self, tmp_path):
        """The other half of the honesty test: no manifest, no manifest tier
        — the call is exactly today's, so the result is exactly today's."""
        _write_tree(tmp_path, legacy=LEGACY_DECLARATION)
        standalone = clone_and_exec._load_sources_module()
        with patch.object(standalone, "load_sources", wraps=standalone.load_sources) as spy:
            rc, _ = _resolve(tmp_path, {"LMER_WORK_REPO_PATH": "/work"})
        assert rc == 0
        spy.assert_called_once_with(tmp_path / "sources.yaml", WORK_URL)

    def test_the_fold_keeps_env_deltas_and_clones_identical_through_main(
        self, tmp_path, monkeypatch
    ):
        """R7's E half: ONE declaration read from sources.yaml; then folded
        into `_plexus.yaml sources:` with sources.yaml removed (what `work
        migrate --instance` does); then a main() container start on that
        folded tree. All three produce identical env deltas and identical
        clones — the fold cannot switch declared sources off."""
        pre = {"LMER_WORK_REPO_PATH": "/work"}

        # (1) The legacy file.
        legacy = _write_tree(tmp_path / "legacy", legacy=LEGACY_DECLARATION)
        env1, calls1 = dict(pre), []
        rc, _ = _resolve(legacy, env1, clone=_recording_clone(calls1))
        assert rc == 0
        delta1 = {k: v for k, v in env1.items() if pre.get(k) != v}

        # (2) Folded: the same bytes under the manifest, no sources.yaml.
        folded = _write_tree(tmp_path / "folded", manifest=_manifest(SOURCES_BLOCK))
        assert not (folded / "sources.yaml").exists()
        env2, calls2 = dict(pre), []
        rc, _ = _resolve(folded, env2, clone=_recording_clone(calls2))
        assert rc == 0
        delta2 = {k: v for k, v in env2.items() if pre.get(k) != v}
        assert delta2 == delta1 == DECLARED_DELTA
        assert calls2 == calls1 == DECLARED_CLONES

        # (3) main() on the folded tree.
        work = tmp_path / "work"
        calls3 = []

        def fake_ensure_clone(workspace, repo_url, branch, ref, **kwargs):
            if Path(workspace) == work:
                # Only the work repo is materialized; the declared /taskdef
                # and /napkin clones must not touch the real filesystem root.
                _write_tree(work, manifest=_manifest(SOURCES_BLOCK))
                return None
            calls3.append((Path(workspace), repo_url, ref))
            return None

        for var in (
            "LMER_SERVICE_MODE", "LMER_REPO_URL", "LMER_CHECKOUT_BRANCH",
            "LMER_CHECKOUT_REF", "GITLAB_MR_ID", "LMER_SECONDARY_TARGETS",
            "LMER_NAPKIN_REPO", "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF",
            "LMER_TASKDEF_PATHS", "LMER_NAPKIN_PATH", "LMER_TRUST_MISE",
            "LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_TASK",
            "LMER_TASK_TARGET",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LMER_NO_REPO", "1")
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # headless
        monkeypatch.setattr(clone_and_exec, "ensure_clone", fake_ensure_clone)
        monkeypatch.setattr(clone_and_exec, "clone_aux_repos", MagicMock())
        monkeypatch.setattr(clone_and_exec, "setup_napkin_and_links", MagicMock())
        monkeypatch.setattr(clone_and_exec, "ensure_work_repo_directory", MagicMock())
        monkeypatch.setattr(clone_and_exec, "provision_documentation", MagicMock())
        monkeypatch.setattr(clone_and_exec, "_trust_mise_config", MagicMock())
        monkeypatch.setattr(clone_and_exec.os, "execv", MagicMock())

        with patch.dict(os.environ):  # restore resolution's direct mutations
            rc = clone_and_exec.main(["--", "true"])
            delta3 = {k: os.environ.get(k) for k in SOURCE_ENV_VARS}

        assert rc == 0
        assert delta3 == delta1
        assert calls3 == calls1

    def test_manifest_with_no_sources_and_no_legacy_file_is_zero_output(self, tmp_path, capsys):
        """The schema-form twin of a tree with no sources.yaml: a manifest
        that declares nothing never engages the matrix — zero output and
        the env byte-identical, even with a source env var set."""
        _write_tree(tmp_path, manifest=_manifest())
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        env = dict(pre)
        rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 0
        assert env == pre
        mock_clone.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_manifest_beats_legacy_with_the_stale_warning(self, tmp_path, capsys):
        other_taskdef = "https://git.example.com/org/taskdefs-v2.git"
        _write_tree(
            tmp_path,
            manifest=_manifest(f"  taskdef:\n    repo: {other_taskdef}\n"),
            legacy=LEGACY_DECLARATION,
        )
        env = {"LMER_WORK_REPO_PATH": "/work"}
        calls = []
        rc, _ = _resolve(tmp_path, env, clone=_recording_clone(calls))
        assert rc == 0
        # Whole entry: the manifest's taskdef declares no ref, so the legacy
        # ref is not inherited; napkin, legacy-only, keeps the legacy entry.
        assert env == {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": other_taskdef,
            "LMER_TASKDEF_PATHS": "/taskdef",
            "LMER_NAPKIN_REPO": DECLARED_NAPKIN,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        assert calls == [
            (Path("/taskdef"), f"https://oauth2:{TOKEN}@git.example.com/org/taskdefs-v2.git", None),
            (Path("/napkin"), NAPKIN_CLONE_URL, None),
        ]
        err = capsys.readouterr().err
        # The loader's stale-legacy WARN is printed...
        assert (
            f"⚠️  {tmp_path / 'sources.yaml'}: `sources.taskdef` is also declared in "
            f"{tmp_path / MANIFEST_NAME}, which takes precedence"
        ) in err
        # ...and each row names the tier it came from.
        assert f"source taskdef: {other_taskdef} (declared (manifest))\n" in err
        assert f"source napkin: {DECLARED_NAPKIN} (declared (sources.yaml))\n" in err
        assert TOKEN not in err

    def test_manifest_warnings_are_the_validators_and_never_printed(self, tmp_path, capsys):
        """`Manifest.warnings` (unknown keys anywhere in the manifest) belong
        to `lmer-schema validate`; only the loader's `sources:` findings
        are printed here, so nothing is repeated in two spellings."""
        _write_tree(
            tmp_path,
            manifest=_manifest(
                f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n    extra: 1\n",
                extra="future_key: 1\n",
            ),
        )
        rc, _ = _resolve(tmp_path, {"LMER_WORK_REPO_PATH": "/work"})
        assert rc == 0
        err = capsys.readouterr().err
        # The loader's finding, in its spelling...
        assert "unknown key `extra` under `sources.taskdef`" in err
        # ...and neither of the core's.
        assert "future_key" not in err
        assert "sources.taskdef.extra" not in err

    def test_env_equal_to_the_manifest_is_a_silent_env_match(self, tmp_path, capsys):
        """Env beats manifest, row 2: the credentialed form of the manifest's
        declaration wins silently — no prompt, env byte-identical, the env
        URL cloned, the banner's origin env-match (no file named: the value
        in effect came from the env)."""
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": ENV_TASKDEF_EQUAL,
            "LMER_TASKDEF_PATHS": "/mnt/td0:/taskdef",
        }
        env = dict(pre)
        calls = []
        rc, _ = _resolve(tmp_path, env, clone=_recording_clone(calls))
        assert rc == 0
        assert env == pre
        assert calls == [(Path("/taskdef"), ENV_TASKDEF_EQUAL, None)]
        err = capsys.readouterr().err
        assert err == f"source taskdef: {DECLARED_TASKDEF} (env-match)\n"

    def test_env_different_from_the_manifest_prompts_on_a_tty_naming_the_manifest(
        self, tmp_path, capsys
    ):
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        calls = []
        rc, _ = _resolve(
            tmp_path, env, isatty=True, input_fn=_scripted_input(["d"]),
            clone=_recording_clone(calls),
        )
        assert rc == 0
        err = capsys.readouterr().err
        assert f"     {MANIFEST_NAME} declares: {DECLARED_TASKDEF}\n" in err
        assert "sources.yaml declares" not in err
        assert f"source taskdef: {DECLARED_TASKDEF} (declared (manifest))\n" in err
        assert calls == [(Path("/taskdef"), ENV_TASKDEF_EQUAL, None)]
        assert TOKEN not in err

    def test_env_different_from_the_manifest_headless_exits_2_naming_the_manifest(
        self, tmp_path, capsys
    ):
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        pre = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_REPO": ENV_TASKDEF_OTHER}
        env = dict(pre)
        rc, mock_clone = _resolve(tmp_path, env)
        assert rc == 2
        mock_clone.assert_not_called()
        assert env == pre
        err = capsys.readouterr().err
        assert err == (
            f"❌ source taskdef: repo mismatch — {MANIFEST_NAME} declares "
            f"{DECLARED_TASKDEF} but LMER_TASKDEF_REPO is set to "
            f"https://git.example.com/org/other.git. Fix: update {MANIFEST_NAME}, "
            "change or unset LMER_TASKDEF_REPO to match, or launch interactively "
            "to choose.\n"
        )

    def test_env_only_note_names_both_files_searched_when_a_manifest_is_present(
        self, tmp_path, capsys
    ):
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        pre = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        env = dict(pre)
        rc, _ = _resolve(tmp_path, env)
        assert rc == 0
        err = capsys.readouterr().err
        assert (
            f"ℹ️  source napkin: no declaration in {MANIFEST_NAME} or sources.yaml — "
            "using LMER_NAPKIN_REPO (env-only)\n"
        ) in err
        assert "source napkin: https://git.example.com/org/other-napkin.git (env-only)\n" in err
        # The env-only row's env stays byte-identical.
        assert {k: env[k] for k in ("LMER_NAPKIN_REPO", "LMER_NAPKIN_PATH")} == {
            k: pre[k] for k in ("LMER_NAPKIN_REPO", "LMER_NAPKIN_PATH")
        }

    def test_ref_env_only_note_names_the_file_the_row_came_from(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        env = {"LMER_WORK_REPO_PATH": "/work", "LMER_TASKDEF_REF": "dev"}
        rc, _ = _resolve(tmp_path, env)
        assert rc == 0
        assert env["LMER_TASKDEF_REF"] == "dev"
        assert (
            f"ℹ️  source taskdef: no ref declared in {MANIFEST_NAME} — using "
            "LMER_TASKDEF_REF (env-only)\n"
        ) in capsys.readouterr().err

    def test_declared_clone_failure_remediation_names_the_manifest(self, tmp_path, capsys):
        _write_tree(tmp_path, manifest=_manifest(f"  taskdef:\n    repo: {DECLARED_TASKDEF}\n"))
        rc, _ = _resolve(
            tmp_path, {"LMER_WORK_REPO_PATH": "/work"},
            clone=_recording_clone([], failing="/taskdef"),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert f"or remove the taskdef declaration from {MANIFEST_NAME}." in err
        assert "from sources.yaml" not in err

    def test_a_legacy_row_under_a_manifest_names_the_legacy_file(self, tmp_path, capsys):
        """Per row, not per tree: with a manifest present, a source only the
        legacy file declares is labelled as the legacy file's."""
        _write_tree(
            tmp_path,
            manifest=_manifest(f"  napkin:\n    repo: {DECLARED_NAPKIN}\n"),
            legacy=f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n",
        )
        rc, _ = _resolve(
            tmp_path, {"LMER_WORK_REPO_PATH": "/work"},
            clone=_recording_clone([], failing="/taskdef"),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert f"source taskdef: {DECLARED_TASKDEF} (declared (sources.yaml))\n" in err
        assert f"source napkin: {DECLARED_NAPKIN} (declared (manifest))\n" in err
        assert "or remove the taskdef declaration from sources.yaml." in err

    def test_a_tree_without_a_manifest_keeps_the_legacy_vocabulary(self, tmp_path, capsys):
        """No manifest: the banner is the bare `(declared)`, the notes name
        sources.yaml alone, and `_plexus.yaml` is never mentioned — today's
        output, byte for byte."""
        _write_tree(tmp_path, legacy=f"schema: 1\nsources:\n  taskdef:\n    repo: {DECLARED_TASKDEF}\n")
        env = {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REF": "dev",
            "LMER_NAPKIN_REPO": ENV_NAPKIN_OTHER,
            "LMER_NAPKIN_PATH": "/napkin",
        }
        rc, _ = _resolve(tmp_path, env)
        assert rc == 0
        assert capsys.readouterr().err == (
            "ℹ️  source taskdef: no ref declared in sources.yaml — using LMER_TASKDEF_REF (env-only)\n"
            "ℹ️  source napkin: no declaration in sources.yaml — using LMER_NAPKIN_REPO (env-only)\n"
            f"source taskdef: {DECLARED_TASKDEF} (declared)\n"
            "source napkin: https://git.example.com/org/other-napkin.git (env-only)\n"
        )


class TestManifestTierDriftGuards:
    def test_manifest_name_mirrors_the_core_and_the_loader(self):
        """The stdlib probe spells the name the core owns; the loader's
        message-label mirror is the same name."""
        assert clone_and_exec.PLEXUS_MANIFEST_NAME == manifest_core.MANIFEST_NAME
        assert clone_and_exec.PLEXUS_MANIFEST_NAME == sources_mod.MANIFEST_FILENAME

    def test_declared_origin_mirrors_the_loader(self):
        """ORIGIN_DECLARED is the legacy-only banner spelling; the tiered
        label the loader spells wraps the same bare word."""
        assert clone_and_exec.ORIGIN_DECLARED == sources_mod.ORIGIN_DECLARED
        for tier in sources_mod.SOURCE_TIERS:
            assert sources_mod.declared_origin_label(tier).startswith(
                f"{clone_and_exec.ORIGIN_DECLARED} ("
            )

    def test_load_manifest_is_the_only_reader_of_the_manifest(self, tmp_path, monkeypatch, capsys):
        """No second manifest parser on this path: with the core's
        load_manifest monkeypatched, the on-disk `_plexus.yaml` can be
        unparseable and the fake's `sources:` is what resolves — nothing else
        opened the file. The fake's `warnings` never reach the output."""
        _write_tree(tmp_path, manifest="schema: 1\nname: example\nsources: [oops\n")
        seen = []

        def fake_load_manifest(root):
            seen.append(Path(root))
            return manifest_core.Manifest(
                path=Path(root) / MANIFEST_NAME,
                schema=1,
                name="example",
                projects={},
                sources={"taskdef": {"repo": DECLARED_TASKDEF}},
                housekeeping={},
                warnings=("VALIDATOR-ONLY-WARNING",),
            )

        monkeypatch.setattr(manifest_core, "load_manifest", fake_load_manifest)
        env = {"LMER_WORK_REPO_PATH": "/work"}
        calls = []
        rc, _ = _resolve(tmp_path, env, clone=_recording_clone(calls))
        assert rc == 0
        assert seen == [tmp_path]
        assert env == {
            "LMER_WORK_REPO_PATH": "/work",
            "LMER_TASKDEF_REPO": DECLARED_TASKDEF,
            "LMER_TASKDEF_PATHS": "/taskdef",
        }
        assert calls == [(Path("/taskdef"), ENV_TASKDEF_EQUAL, None)]
        assert "VALIDATOR-ONLY-WARNING" not in capsys.readouterr().err


class TestStandaloneEntrypointSources:
    """The container runs this file BY PATH (``python3 .../clone_and_exec.py``)
    — the context the module's import contract is about. Both tiers are
    exercised once that way, in a ``-P`` subprocess that loads the file by
    location (tests/test_clone_and_exec_session_dir.py's pattern)."""

    def _run(self, tmp_path, root, expected_env, expected_clones, body):
        script = textwrap.dedent(
            f"""
            import importlib.util
            import sys
            from pathlib import Path

            spec = importlib.util.spec_from_file_location(
                "lmer_container_entrypoint", {str(ENTRYPOINT_PATH)!r}
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["lmer_container_entrypoint"] = module
            spec.loader.exec_module(module)
            assert "lmer_cli" not in sys.modules, "loading the entrypoint imported the package"
            assert "lmer_schema" not in sys.modules, "loading the entrypoint imported the core"

            clones = []
            module.ensure_clone = lambda dest, url, branch, ref, **kw: clones.append(
                (str(dest), url, ref)
            )
            env = {{"LMER_WORK_REPO_PATH": "/work"}}
            rc = module._resolve_declared_sources(
                Path({str(root)!r}), {WORK_URL!r}, env=env, isatty=False
            )
            assert rc == 0, rc
            assert env == {expected_env!r}, env
            assert clones == {expected_clones!r}, clones
            """
        ) + textwrap.dedent(body) + "\nprint('OK')\n"
        result = subprocess.run(
            [sys.executable, "-P", "-c", script],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        return result

    def test_a_legacy_tree_resolves_without_importing_the_core(self, tmp_path):
        root = _write_tree(tmp_path / "work", legacy=LEGACY_DECLARATION)
        self._run(
            tmp_path, root,
            {"LMER_WORK_REPO_PATH": "/work", **DECLARED_DELTA},
            [(str(dest), url, ref) for dest, url, ref in DECLARED_CLONES],
            """
            assert "lmer_schema" not in sys.modules, "a legacy tree imported the core"
            assert "lmer_cli" not in sys.modules, "a legacy tree imported the package"
            """,
        )

    def test_a_manifest_tree_resolves_through_the_core_by_path(self, tmp_path):
        """The guarded function-local import works in the by-path context
        too (the core is installed in the image): the manifest tier resolves
        through the core. The core's package body eagerly pulls
        ``lmer_cli.container.sources`` (its D21 URL adapter — the dependency
        points one way, by the core's own contract), so ``lmer_cli`` IS in
        sys.modules afterwards; that is the core's import, not the
        entrypoint's, whose own contract the AST guard and the legacy-tree
        test above hold."""
        root = _write_tree(tmp_path / "work", manifest=_manifest(SOURCES_BLOCK))
        result = self._run(
            tmp_path, root,
            {"LMER_WORK_REPO_PATH": "/work", **DECLARED_DELTA},
            [(str(dest), url, ref) for dest, url, ref in DECLARED_CLONES],
            """
            assert "lmer_schema.manifest" in sys.modules, "the manifest tier was not read through the core"
            """,
        )
        assert f"source taskdef: {DECLARED_TASKDEF} (declared (manifest))" in result.stderr
