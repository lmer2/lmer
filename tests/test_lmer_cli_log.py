#!/usr/bin/env python3
"""Tests for lmer_cli.log module"""

import os
import pytest
from io import StringIO
import sys
from unittest.mock import patch

from lmer_cli.log import is_verbose, info, success, error, warning


class TestLogVerbosity:
    """Test verbose mode detection"""

    def test_is_verbose_when_set_to_1(self):
        """Test verbose mode is enabled when LMER_VERBOSE=1"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            assert is_verbose() is True

    def test_is_verbose_when_set_to_true(self):
        """Test verbose mode is enabled when LMER_VERBOSE=true"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "true"}):
            assert is_verbose() is True

    def test_is_verbose_when_set_to_True(self):
        """Test verbose mode is enabled when LMER_VERBOSE=True"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "True"}):
            assert is_verbose() is True

    def test_is_verbose_when_set_to_0(self):
        """Test verbose mode is disabled when LMER_VERBOSE=0"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "0"}):
            assert is_verbose() is False

    def test_is_verbose_when_not_set(self):
        """Test verbose mode is disabled when LMER_VERBOSE not set"""
        with patch.dict(os.environ, {}, clear=True):
            assert is_verbose() is False

    def test_is_verbose_when_set_to_invalid(self):
        """Test verbose mode is disabled for invalid values"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "yes"}):
            assert is_verbose() is False


class TestLogFunctions:
    """Test logging functions"""

    def test_info_prints_when_verbose(self, capsys):
        """Test info() prints message when verbose mode enabled"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            info("test message")
            captured = capsys.readouterr()
            assert "test message" in captured.out

    def test_info_silent_when_not_verbose(self, capsys):
        """Test info() is silent when verbose mode disabled"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "0"}):
            info("test message")
            captured = capsys.readouterr()
            assert "test message" not in captured.out

    def test_success_always_prints(self, capsys):
        """Test success() always prints regardless of verbosity"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "0"}):
            success("success message")
            captured = capsys.readouterr()
            assert "success message" in captured.out

    def test_error_always_prints(self, capsys):
        """Test error() always prints regardless of verbosity"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "0"}):
            error("error message")
            captured = capsys.readouterr()
            assert "error message" in captured.out

    def test_warning_always_prints(self, capsys):
        """Test warning() always prints regardless of verbosity"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "0"}):
            warning("warning message")
            captured = capsys.readouterr()
            assert "warning message" in captured.out

    def test_multiple_messages(self, capsys):
        """Test multiple log messages"""
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            info("info 1")
            success("success 1")
            error("error 1")
            warning("warning 1")
            info("info 2")

            captured = capsys.readouterr()
            assert "info 1" in captured.out
            assert "success 1" in captured.out
            assert "error 1" in captured.out
            assert "warning 1" in captured.out
            assert "info 2" in captured.out


class TestLogFlush:
    """Output must be flushed immediately (block-buffered stdout regression).

    print() is block-buffered when stdout is not a TTY, so anyone wrapping
    `lmer` in tee/timeout/pipe sees nothing until the process exits. The
    helpers must pass flush=True to print().
    """

    def test_info_flushes_stdout(self):
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            with patch("lmer_cli.log.print") as mock_print:
                info("hello")
                mock_print.assert_called_once_with("hello", flush=True)

    def test_success_flushes_stdout(self):
        with patch("lmer_cli.log.print") as mock_print:
            success("ok")
            mock_print.assert_called_once_with("ok", flush=True)

    def test_error_flushes_stdout(self):
        with patch("lmer_cli.log.print") as mock_print:
            error("boom")
            mock_print.assert_called_once_with("boom", flush=True)

    def test_warning_flushes_stdout(self):
        with patch("lmer_cli.log.print") as mock_print:
            warning("careful")
            mock_print.assert_called_once_with("careful", flush=True)
