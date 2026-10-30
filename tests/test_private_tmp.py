"""Owner-only state under a shared /tmp (src/lmer_cli/private_tmp.py, #262)."""
import os
import stat

import pytest

from lmer_cli import private_tmp


class TestOwnedDir:
    def test_a_missing_dir_is_created_owner_only(self, tmp_path):
        directory = tmp_path / "private"
        assert private_tmp.owned_dir(directory, create=True) == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_missing_dir_is_not_created_without_create(self, tmp_path):
        assert private_tmp.owned_dir(tmp_path / "private") is None
        assert not (tmp_path / "private").exists()

    def test_a_dir_it_created_is_usable_under_a_narrow_umask(self, tmp_path):
        """The created dir is ours, so it gets exactly the mode — a umask
        that also stripped the owner's write bit must not leave it read-only."""
        old = os.umask(0o277)
        try:
            directory = private_tmp.owned_dir(tmp_path / "private", create=True)
        finally:
            os.umask(old)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_looser_bits_are_tightened(self, tmp_path):
        directory = tmp_path / "private"
        directory.mkdir()
        directory.chmod(0o777)
        assert private_tmp.owned_dir(directory) == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_tighter_bits_are_never_widened(self, tmp_path):
        directory = tmp_path / "private"
        directory.mkdir(mode=0o500)
        assert private_tmp.owned_dir(directory) == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o500

    def test_a_symlink_is_refused_not_followed(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        real.chmod(0o755)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert private_tmp.owned_dir(link, create=True) is None
        assert stat.S_IMODE(real.stat().st_mode) == 0o755

    def test_a_file_is_refused(self, tmp_path):
        path = tmp_path / "file"
        path.write_text("x")
        assert private_tmp.owned_dir(path, create=True) is None

    def test_another_uids_dir_is_refused_and_left_alone(self, tmp_path, monkeypatch):
        directory = tmp_path / "theirs"
        directory.mkdir()
        directory.chmod(0o755)
        other = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other)
        assert private_tmp.owned_dir(directory, create=True) is None
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755


class TestOpenPrivate:
    def test_creates_owner_only(self, tmp_path):
        path = tmp_path / "file"
        private_tmp.write_private(path, "hello\n")
        assert path.read_text() == "hello\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_append_keeps_what_was_there(self, tmp_path):
        path = tmp_path / "file"
        private_tmp.write_private(path, "a\n", append=True)
        private_tmp.write_private(path, "b\n", append=True)
        assert path.read_text() == "a\nb\n"

    def test_a_symlink_is_never_written_through(self, tmp_path):
        target = tmp_path / "victim"
        target.write_text("untouched\n")
        link = tmp_path / "file"
        link.symlink_to(target)
        with pytest.raises(OSError):
            private_tmp.write_private(link, "planted\n")
        assert target.read_text() == "untouched\n"

    def test_another_uids_file_is_refused_before_truncation(
        self, tmp_path, monkeypatch
    ):
        """O_TRUNC is applied only after the ownership check: refusing a
        file someone else owns after emptying it would be no refusal."""
        path = tmp_path / "file"
        path.write_text("theirs\n")
        other = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other)
        with pytest.raises(PermissionError):
            private_tmp.write_private(path, "mine\n")
        assert path.read_text() == "theirs\n"

    def test_a_fifo_is_refused_without_hanging(self, tmp_path):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(OSError):
            private_tmp.write_private(fifo, "x\n")
        with pytest.raises(OSError):
            private_tmp.read_no_follow(fifo)

    def test_read_refuses_a_symlink(self, tmp_path):
        target = tmp_path / "target"
        target.write_text("x")
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(OSError):
            private_tmp.read_no_follow(link)
        assert private_tmp.read_no_follow(target) == "x"

    def test_another_uids_file_is_not_read(self, tmp_path, monkeypatch):
        """Contents someone else planted are not this uid's to believe."""
        path = tmp_path / "file"
        path.write_text("planted\n")
        other = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other)
        with pytest.raises(PermissionError):
            private_tmp.read_no_follow(path)
