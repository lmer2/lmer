#!/usr/bin/env python3
"""Tests for work_repo.loggers module"""

import os
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest.mock import patch

from tests.conftest import denied_read
from work_repo.loggers import Logger, YamlFileLogger, get_logger


class TestYamlFileLogger:
    """Test YAML file logger"""

    def test_log_creates_file(self):
        """Test logging creates log file if it doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message")

            assert log_file.exists()
            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert len(logs) == 1
                assert logs[0]["message"] == "Test message"
                assert "timestamp" in logs[0]

    def test_log_appends_to_existing_file(self):
        """Test logging appends to existing log file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("First message")
            logger.log("Second message")

            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert len(logs) == 2
                assert logs[0]["message"] == "First message"
                assert logs[1]["message"] == "Second message"

    def test_log_with_metadata(self):
        """Test logging with metadata"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message", {"status": "success", "duration": "5m"})

            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert logs[0]["message"] == "Test message"
                assert logs[0]["status"] == "success"
                assert logs[0]["duration"] == "5m"

    def test_log_creates_parent_directories(self):
        """Test logging creates parent directories if needed"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nested" / "path" / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message")

            assert log_file.exists()
            assert log_file.parent.exists()

    def test_log_refuses_to_append_over_corrupted_yaml(self):
        """Corrupt input is refused and preserved, not written over.

        This test used to assert the opposite, and it is worth saying so: it
        was called `test_log_handles_corrupted_yaml`, it wrote a bad file, it
        called `log()`, and it asserted the result was "a list with at least
        the new message" — which is precisely the wipe. "Handled gracefully"
        described a `log()` that had just replaced a run's whole history with
        one entry and returned success.

        The behaviour is now `release.yaml`'s: back the bad bytes up and raise.
        See `TestACorruptLogIsPreservedNotWiped` below for what that protects.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            corrupt = "invalid: yaml: content: [\n"
            log_file.write_text(corrupt, encoding="utf-8")

            with pytest.raises(Exception):
                YamlFileLogger(log_file).log("New message")

            backups = [p for p in Path(tmpdir).iterdir() if ".bad-" in p.name]
            assert len(backups) == 1
            assert backups[0].read_text(encoding="utf-8") == corrupt


class TestGetLogger:
    """Test get_logger function"""

    def test_get_logger_with_explicit_path(self):
        """Test get_logger with explicit log file path"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = get_logger(log_file)

            assert isinstance(logger, YamlFileLogger)
            assert logger.log_file == log_file

    def test_get_logger_from_env_vars(self):
        """Test get_logger determines path from environment variables"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                logger = get_logger()

                assert isinstance(logger, YamlFileLogger)
                expected_path = Path(tmpdir) / "github.com" / "owner" / "repo" / "runs" / "review-pr-123" / "log.yaml"
                assert logger.log_file == expected_path

    def test_get_logger_missing_env_vars(self):
        """Test get_logger raises error when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="LMER_REPO_HOST"):
                get_logger()

    def test_get_logger_unknown_type(self):
        """Test get_logger with unknown logger type"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            with patch.dict(os.environ, {"WORK_LOGGER_TYPE": "unknown"}):
                with pytest.raises(ValueError, match="Unknown logger type"):
                    get_logger(log_file)


class TestACorruptLogIsPreservedNotWiped:
    """An append must never rewrite the file from an empty list.

    `log()` reads the whole file, appends, and writes it all back. Answering an
    unparseable or non-list file with `[]` therefore did not skip the bad
    entry — it replaced every entry in the run's worklog with the single new
    one, and `work log` printed its success tick over the top. Reproduced at
    175 bytes / 2 entries in, 87 bytes / 1 entry out.

    Nothing warned beforehand: `cli.render_truncated_log` prints raw lines and
    never parses, so the file read fine until the next append emptied it.

    The shape here is `release.yaml`'s (`run_state._backup_bad_state`, spec §6):
    move the bad bytes aside and raise. `cmd_log` reports and exits 1, which is
    the outcome that matters — a failed append leaves the history intact and
    says so, where a successful one that ate it does not.
    """

    ORIGINAL = (
        "- timestamp: '2026-08-01T10:00:00Z'\n"
        "  message: the first real entry of this run\n"
        "- timestamp: '2026-08-01T11:00:00Z'\n"
        "  message: the second real entry\n"
        "  metadata: [unterminated\n"
    )

    def test_an_unparseable_log_is_backed_up_and_the_append_refuses(self, tmp_path):
        log_file = tmp_path / "log.yaml"
        log_file.write_text(self.ORIGINAL, encoding="utf-8")

        with pytest.raises(Exception, match="could not be parsed"):
            YamlFileLogger(log_file).log("a new entry appended by work log")

        backups = [p for p in tmp_path.iterdir() if ".bad-" in p.name]
        assert len(backups) == 1, "the bad bytes were not preserved"
        assert backups[0].read_text(encoding="utf-8") == self.ORIGINAL, (
            "the backup is not byte-identical to what was on disk"
        )

    def test_the_entries_are_not_replaced_by_the_new_one(self, tmp_path):
        """The assertion that actually names the damage: whatever else happens,
        the two real entries must not have become one."""
        log_file = tmp_path / "log.yaml"
        log_file.write_text(self.ORIGINAL, encoding="utf-8")

        with pytest.raises(Exception):
            YamlFileLogger(log_file).log("a new entry appended by work log")

        surviving = "".join(
            p.read_text(encoding="utf-8") for p in tmp_path.iterdir()
        )
        assert "the first real entry of this run" in surviving
        assert "the second real entry" in surviving

    def test_a_log_that_is_not_a_list_is_treated_the_same(self, tmp_path):
        """A mapping where a list belongs is corruption too — and it used to be
        the quieter half, since it parses cleanly and then reads as `[]`."""
        log_file = tmp_path / "log.yaml"
        log_file.write_text("message: someone wrote a mapping here\n", encoding="utf-8")

        with pytest.raises(Exception, match="not the list of entries"):
            YamlFileLogger(log_file).log("a new entry")

        backups = [p for p in tmp_path.iterdir() if ".bad-" in p.name]
        assert len(backups) == 1
        assert "someone wrote a mapping here" in backups[0].read_text(encoding="utf-8")

    def test_an_empty_file_is_the_first_append_not_corruption(self, tmp_path):
        """`yaml.safe_load("")` is None, and a run whose log file exists but
        holds nothing has simply not been written to yet."""
        log_file = tmp_path / "log.yaml"
        log_file.write_text("", encoding="utf-8")

        YamlFileLogger(log_file).log("the first entry")

        entries = yaml.safe_load(log_file.read_text(encoding="utf-8"))
        assert [e["message"] for e in entries] == ["the first entry"]
        assert not [p for p in tmp_path.iterdir() if ".bad-" in p.name]

    def test_an_unreadable_log_is_refused_without_being_moved(self, tmp_path):
        """The distinction the docstring puts the most weight on, and the one
        branch nothing pinned (!271 review).

        A file this process cannot read RIGHT NOW is not a file we know to be
        corrupt. Backing it up would rename a live log out from under whoever
        holds it, turning a transient permission or IO problem into a moved
        log — so this branch raises unbacked, and every future edit that
        inverts it now goes red.
        """
        log_file = tmp_path / "log.yaml"
        log_file.write_text("- {message: keep me}\n", encoding="utf-8")
        before = log_file.read_bytes()

        # `denied_read`, not `chmod(0o000)`: CI runs as root, and root is
        # exempt from the mode bits, so a chmod-based test cannot create the
        # condition it is about — it would read the file happily and fail on
        # the raise that never came. The helper patches `Path.open` (which
        # `read_text` goes through) and denies at any uid.
        with denied_read(log_file):
            with pytest.raises(RuntimeError, match="could not be read"):
                YamlFileLogger(log_file).log("a new entry")

        assert log_file.read_bytes() == before
        assert not [p for p in tmp_path.iterdir() if ".bad-" in p.name], (
            "a file that was merely unreadable was moved aside"
        )

    def test_undecodable_bytes_are_refused_without_being_moved(self, tmp_path):
        """Same branch, reached the other way in."""
        log_file = tmp_path / "log.yaml"
        log_file.write_bytes(b"\xff\xfe not utf-8 at all\n")
        before = log_file.read_bytes()

        with pytest.raises(RuntimeError, match="could not be read"):
            YamlFileLogger(log_file).log("a new entry")

        assert log_file.read_bytes() == before
        assert not [p for p in tmp_path.iterdir() if ".bad-" in p.name]

    def test_a_healthy_log_still_appends(self, tmp_path):
        """The converse guard: refusing corruption must not refuse the ordinary
        append, which is every append."""
        log_file = tmp_path / "log.yaml"
        YamlFileLogger(log_file).log("one")
        YamlFileLogger(log_file).log("two")

        entries = yaml.safe_load(log_file.read_text(encoding="utf-8"))
        assert [e["message"] for e in entries] == ["one", "two"]


class TestThePublishIsAtomic:
    """A partial write must not truncate the live log.

    The parse-corruption fix above catches a log that cannot be read. This is
    the half it does not reach and that the class docstring names as its first
    trigger: `open(path, "w")` truncates first and dumps second, so a kill
    mid-dump, an ENOSPC, or a second writer left a PREFIX of the file — and
    `yaml.dump` emits one entry per block, so a prefix cut at an entry boundary
    is valid YAML and a valid list, merely shorter (!271 review).

    That is strictly worse than the corruption already refused, because it is
    invisible: `_read_entries` parses it happily, the next append republishes
    the truncated history, and no `.bad-` copy is left to recover from.
    """

    @staticmethod
    def _messages(log_file):
        return [e["message"] for e in yaml.safe_load(log_file.read_text())]

    def test_a_write_that_fails_mid_dump_leaves_the_log_untouched(self, tmp_path):
        log_file = tmp_path / "log.yaml"
        for msg in ("one", "two", "three", "four", "five"):
            YamlFileLogger(log_file).log(msg)
        before = log_file.read_bytes()

        class Boom(RuntimeError):
            pass

        real_dump = yaml.dump

        def dump_then_die(data, stream, **kw):
            real_dump(data[:2], stream, **kw)  # a prefix — valid YAML, shorter
            stream.flush()
            raise Boom("killed mid-dump")

        with patch.object(yaml, "dump", dump_then_die):
            with pytest.raises(Boom):
                YamlFileLogger(log_file).log("the entry that never landed")

        assert log_file.read_bytes() == before, (
            "an interrupted write truncated the live log"
        )
        assert self._messages(log_file) == ["one", "two", "three", "four", "five"]

    def test_a_failed_write_leaves_no_temp_file_behind(self, tmp_path):
        """The temp must not be mistaken for a log, or linger as litter."""
        log_file = tmp_path / "log.yaml"
        YamlFileLogger(log_file).log("one")

        def boom(*a, **kw):
            raise RuntimeError("no space left on device")

        with patch.object(yaml, "dump", boom):
            with pytest.raises(RuntimeError):
                YamlFileLogger(log_file).log("two")

        assert sorted(p.name for p in tmp_path.iterdir()) == ["log.yaml"]

    def test_the_live_file_is_never_the_one_being_written(self, tmp_path):
        """The property that makes the two above true rather than lucky: the
        dump goes to a different path, and the live file is only ever replaced
        whole."""
        log_file = tmp_path / "log.yaml"
        YamlFileLogger(log_file).log("one")
        written_to = []
        real_open = open

        def record_open(path, mode="r", *a, **kw):
            if "w" in mode:
                written_to.append(Path(path))
            return real_open(path, mode, *a, **kw)

        with patch("builtins.open", record_open):
            YamlFileLogger(log_file).log("two")

        assert written_to, "nothing was written"
        assert log_file not in written_to, (
            f"the live log was opened for writing: {written_to}"
        )
        assert self._messages(log_file) == ["one", "two"]
