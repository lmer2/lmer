"""Tests for the model a session runs: ``--model`` / LMER_LLM_NAME → claude --model.

Covers three layers:
1. Python (cli.py): the ``--model`` flag (T51) is the flag spelling of a variable
   that until then could only be exported. It is applied to ``os.environ``
   because that is where every consumer already reads it — the harness
   autoselection hint, the fan-out children, the container env dict — and its
   precedence is the ``--harness`` convention: flag over environment, and over a
   preset's env, which is why it is applied after preset resolution. The env var
   is also declared in the host→container env dict; that is verified by a
   source-level sanity check (the dict is built inline in main(), so we guard
   against accidental removal rather than re-testing the local logic).
2. The report back to a spawning orchestrator: the resolved model is written to
   ``$LMER_PLATFORM_PORTS_FILE``, merged with whatever is already in there, so
   the platform can say which model is driving a run without guessing from its
   own environment (issue #141 T50/T51).
3. Bash (claude-runner.sh): the env var is passed verbatim to claude's
   --model flag. No validation or normalization happens — claude itself
   rejects unknown models. Unset or empty means no flag is passed.
"""
import json
import os
import re
import threading
from pathlib import Path

import pytest

from lmer_cli import cli
from lmer_cli.cli import (
    _apply_model_selection,
    _record_published_ports,
    _record_session_model,
    parse_args,
)
from lmer_cli.harness import LLM_NAME_ENV, resolve_harness_selection
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


def test_cli_env_dict_declares_llm_name():
    """Guard against accidental removal of LMER_LLM_NAME from cli.py's env dict.

    The env dict in main() is constructed inline, so a true unit test would
    require extracting it into a helper. For now a source-level check is
    sufficient to catch drift, with tolerance for formatting changes.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_LLM_NAME["']\s*:\s*os\.environ\.get\(\s*["']LMER_LLM_NAME["']\s*\)"""
    )
    assert pattern.search(source), "LMER_LLM_NAME entry missing from cli.py env dict"


class TestModelFlag:
    """``lmer --model <name>`` (T51), the flag spelling of LMER_LLM_NAME."""

    @pytest.fixture(autouse=True)
    def _no_ambient_model(self, monkeypatch):
        """Unset LMER_LLM_NAME, and make sure monkeypatch will undo what the
        code under test writes.

        ``_apply_model_selection`` assigns to ``os.environ`` by design, and
        ``delenv`` on a variable that was already absent records nothing to
        restore — so without the ``setenv`` first, a value set inside a test
        would outlive it and reach whatever runs next under random ordering.
        """
        monkeypatch.setenv(LLM_NAME_ENV, "")
        monkeypatch.delenv(LLM_NAME_ENV)

    def test_the_parser_takes_it(self):
        namespace, rest = parse_args(["develop", "https://x/y", "--model", "opus"])
        assert namespace.model == "opus"
        assert rest == [], "the flag fell through to the container's command line"

    def test_it_becomes_the_session_s_llm_name(self):
        assert _apply_model_selection("opus", {}) == "opus"
        assert os.environ[LLM_NAME_ENV] == "opus"

    def test_it_beats_an_exported_value(self, monkeypatch):
        """Flag over environment, matching the --harness/LMER_HARNESS convention."""
        monkeypatch.setenv(LLM_NAME_ENV, "exported-model")

        assert _apply_model_selection("gpt-5.5", {}) == "gpt-5.5"
        assert os.environ[LLM_NAME_ENV] == "gpt-5.5"

    def test_no_flag_leaves_the_environment_alone(self, monkeypatch):
        """Absent is not "no model": the exported value is still the answer, and
        it is what gets reported back and forwarded to the container."""
        monkeypatch.setenv(LLM_NAME_ENV, "exported-model")

        assert _apply_model_selection(None, {}) == "exported-model"
        assert os.environ[LLM_NAME_ENV] == "exported-model"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_is_not_a_value(self, monkeypatch, blank):
        """``--model ''`` falls through to the environment rather than clearing
        it, the same way an empty ``--harness`` falls through to LMER_HARNESS."""
        monkeypatch.setenv(LLM_NAME_ENV, "exported-model")

        assert _apply_model_selection(blank, {}) == "exported-model"
        assert os.environ[LLM_NAME_ENV] == "exported-model"

    def test_nothing_anywhere_resolves_to_no_model(self):
        assert _apply_model_selection(None, {}) is None

    def test_surrounding_whitespace_is_stripped_but_the_case_is_not(self):
        """The value ends up as a container env var and then as the harness's own
        --model argument, where a stray space is a model name nobody has. The
        case is the harness's business (see test_no_case_normalization)."""
        assert _apply_model_selection("  Sonnet ", {}) == "Sonnet"

    def test_the_show_env_table_attributes_it_to_the_flag(self):
        """Otherwise that table — whose whole job is telling a host export from a
        .env file — would report a flag-set value as coming from the environment."""
        sources: dict = {}
        _apply_model_selection("opus", sources)

        assert sources[LLM_NAME_ENV] == "--model flag"

    def test_the_model_can_imply_the_harness(self, monkeypatch):
        """The reason this is applied to the environment before harness
        resolution: with no --harness and no LMER_HARNESS, the model name picks
        the CLI that serves it."""
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        _apply_model_selection("gpt-5.6-sol", {})

        assert resolve_harness_selection(None) == ("codex", "model")


class TestModelReportedToThePlatform:
    """What a spawning orchestrator is told about the model (T50/T51).

    It cannot work this out for itself: ``lmer`` applies preset env only over
    keys the environment leaves unset, so a daemon inferring the model from its
    own environment would be wrong for precisely the runs that named a preset.
    """

    def test_nothing_is_written_without_a_ports_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LMER_PLATFORM_PORTS_FILE", raising=False)
        _record_session_model("opus")  # must not raise
        assert list(tmp_path.iterdir()) == []

    def test_the_resolved_model_is_written(self, monkeypatch, tmp_path):
        target = tmp_path / "nested" / "facts.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))

        _record_session_model("claude-opus-5")

        assert json.loads(target.read_text(encoding="utf-8")) == {
            "model": "claude-opus-5"
        }

    def test_no_model_writes_no_fact(self, monkeypatch, tmp_path):
        """Unset means the harness ran its own default, and recording that as a
        fact would put a name on a row that never chose one."""
        target = tmp_path / "facts.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))

        _record_session_model(None)

        assert not target.exists()

    def test_the_ports_written_later_do_not_erase_the_model(
        self, monkeypatch, tmp_path
    ):
        """The order a launch writes them in: the model is settled before the
        container is built, the ports while it starts. A second overwriting write
        would lose the model for exactly the sessions that publish ports."""
        target = tmp_path / "facts.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))

        _record_session_model("opus")
        _record_published_ports([9001], "127.0.0.1")

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["model"] == "opus"
        assert payload["ports"] == [{"host": 9001, "container": 9001}]
        assert payload["bind"] == "127.0.0.1"

    def test_a_corrupt_file_is_replaced_rather_than_failing_the_launch(
        self, monkeypatch, tmp_path
    ):
        target = tmp_path / "facts.json"
        target.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))

        _record_session_model("opus")

        assert json.loads(target.read_text(encoding="utf-8")) == {"model": "opus"}

    def test_an_unwritable_target_is_a_warning_not_a_launch_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(tmp_path / "facts.json"))
        monkeypatch.setattr(
            Path, "write_text",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")),
        )

        _record_session_model("opus")  # must not raise

    def test_main_reports_the_model_it_resolved(self):
        """The call site, guarded at source level like the env dict above: it has
        to sit after preset application (so the reported value is the resolved
        one) and it is the only thing that makes RunView.model answer."""
        source = CLI_PY.read_text()
        applied = source.index("_apply_model_selection(ns.model")
        reported = source.index("_record_session_model(session_model)")

        assert applied < reported, (
            "the model is reported before it is resolved, so a --model flag "
            "would never reach the platform"
        )


#: Facts per writer for the held-writer test below; the second is the shorter.
FACT_ROWS = {"first": 100, "second": 25}


class TestTheFactsFileIsWrittenAtomically:
    """The temp the facts file is published through, and who else may hold one.

    A launch records more than one fact at more than one point — the model before
    the container is built, the ports while it starts — and a spawning platform
    reads this file to say what a run is doing. Two writers deriving one temp path
    is worse than losing a write: the second's truncation lands inside the first's
    file, and the first renames the hole over the target and reports success.
    """

    @pytest.fixture(autouse=True)
    def _facts_file(self, monkeypatch, tmp_path):
        self.target = tmp_path / "facts.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(self.target))
        return self.target

    def test_the_temp_name_carries_the_writers_process_and_thread(self, monkeypatch):
        """The pid stays — it is what keeps two processes apart — and the thread
        id joins it, the shape ``lmer_platform.store.write_json`` settled on."""
        written = []
        real_write_text = Path.write_text

        def capture(self, *args, **kwargs):
            written.append(self)
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", capture)
        _record_session_model("opus")

        assert len(written) == 1, written
        tmp = written[0]
        assert tmp.parent == self.target.parent, "a rename across filesystems is a copy"
        assert tmp.name.startswith("."), "a visible temp reads as the facts file"
        assert tmp.name.endswith(".tmp")
        parts = tmp.name.split(".")
        assert str(os.getpid()) in parts, "two processes must still not collide"
        assert str(threading.get_ident()) in parts, "nor two threads of one process"

    def test_a_second_writer_cannot_hole_what_the_first_publishes(self, monkeypatch):
        """Two writers held at the point of interest, not raced free-running.

        The second is let in once the first has bytes on disk, and holds its temp
        open across the first's rename. With a shared temp name the first
        publishes a file that is part of the second's payload, a run of NULs, and
        the rest of its own — unparseable, so the *next* writer treats it as
        absent and every fact recorded so far is gone.
        """
        first_is_mid_file = threading.Event()
        second_holds_its_temp = threading.Event()
        published = threading.Event()
        payloads, temps, flushed, errors, warnings = {}, {}, [], [], []
        real_open = Path.open

        def gated_write_text(self, data, encoding=None, errors=None, newline=None):
            """:meth:`Path.write_text` with one pause added.

            Same shape as the real one — a single open (which truncates),
            sequential writes, close — split at a flush boundary the buffered
            writer produces on its own for a payload this size. Splitting it here
            makes the moment deterministic; it does not create it.
            """
            name = threading.current_thread().name
            payloads[name] = data
            if name == "second":
                # Nothing to lose until the other writer has bytes on disk.
                first_is_mid_file.wait(timeout=60)
            half = len(data) // 2
            with real_open(self, "w", encoding=encoding) as fh:
                # The truncation is here: with a shared temp name, this is what
                # lands in the middle of the other writer's file.
                fh.write(data[:half])
                fh.flush()
                temps[name] = self
                if name == "first":
                    flushed.append(self.stat().st_size)
                    first_is_mid_file.set()
                    second_holds_its_temp.wait(timeout=60)
                else:
                    second_holds_its_temp.set()
                    published.wait(timeout=60)
                fh.write(data[half:])

        monkeypatch.setattr(Path, "write_text", gated_write_text)
        monkeypatch.setattr(cli, "warning", lambda msg: warnings.append(msg))

        def record(name):
            # The second payload is deliberately the shorter one: its truncation
            # then ends before the first writer's offset, so the bytes between
            # read back as NULs instead of being papered over by a same-sized
            # payload. Both are large enough for the halves to be real.
            facts = {f"{name}{index}": name * 40 for index in range(FACT_ROWS[name])}
            try:
                cli._record_platform_facts(**facts)
            except BaseException as exc:  # pragma: no cover - a failure prints why
                errors.append(exc)

        first = threading.Thread(target=record, args=("first",), name="first")
        second = threading.Thread(target=record, args=("second",), name="second")
        first.start()
        second.start()
        first.join(timeout=120)

        assert not first.is_alive(), "the first writer never published"
        assert second.is_alive(), "the second writer was supposed to still be holding on"
        # Read here: this is the moment a platform poll would land in, with the
        # other writer's fd still open on a file that has been renamed.
        mid_race = self.target.read_bytes()
        published.set()
        second.join(timeout=120)

        assert flushed and flushed[0] > 0, (
            "the first writer had nothing on disk yet, so this proved nothing"
        )
        assert b"\x00" not in mid_race, "a hole is what the second's truncation leaves"
        recorded = json.loads(mid_race)
        assert len(recorded) == FACT_ROWS["first"], "facts lost from a published file"
        assert all(key.startswith("first") for key in recorded), recorded.keys()
        assert mid_race == payloads["first"].encode()
        # The second writer publishes last, whole, and leaves nothing behind.
        assert json.loads(self.target.read_bytes()) == json.loads(payloads["second"])
        assert list(self.target.parent.glob(".*.tmp")) == [], "a temp outlived the race"
        # The mechanism behind all of the above, named last.
        assert temps["first"] != temps["second"], "both writers derived one temp path"
        # And the writers' own verdict: this one reports failure as a warning, so
        # a silent launch is part of the contract being checked.
        assert not errors, errors
        assert not warnings, warnings


def _run_claude_runner(tmp_path, env_value=None):
    env = {} if env_value is None else {"LMER_LLM_NAME": env_value}
    result = run_claude_runner(tmp_path, env)
    return result.output, result.argv


@skip_if_npm_claude_present
class TestClaudeRunnerModelFlag:
    """Verify claude-runner.sh translates LMER_LLM_NAME → --model."""

    @pytest.mark.parametrize("model", ["sonnet", "opus", "haiku", "claude-sonnet-4-6"])
    def test_value_passes_model_flag_verbatim(self, tmp_path, model):
        output, argv = _run_claude_runner(tmp_path, env_value=model)

        assert "--model" in argv, f"--model missing from claude argv: {argv}"
        idx = argv.index("--model")
        assert argv[idx + 1] == model
        assert f"Claude model: {model}" in output

    def test_no_case_normalization(self, tmp_path):
        """The value reaches claude exactly as given — no lowercasing."""
        output, argv = _run_claude_runner(tmp_path, env_value="Sonnet")

        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "Sonnet"

    def test_unset_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value=None)

        assert "--model" not in argv

    def test_empty_string_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="")

        assert "--model" not in argv
