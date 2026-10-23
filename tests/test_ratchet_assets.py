"""Ratchet's faces (issue #581): chadd's art, where the daemon can serve it.

The ten 128x128 transparent PNGs are chadd's final icons and are used exactly as
delivered — never resized or recompressed here. The UI downscales them in CSS.
"""
import struct
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "public" / "assets" / "ratchet"
MOODS = ("question", "oops", "panic", "strained", "on_it", "skeptical", "idle",
         "yes", "talking", "off")


def _ihdr(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, colour


@pytest.mark.parametrize("mood", MOODS)
def test_face_is_a_square_rgba_png(mood):
    path = ASSETS / f"ratchet_{mood}.png"
    assert path.is_file(), f"missing {path}"
    width, height, depth, colour = _ihdr(path)
    assert (width, height) == (128, 128), f"{path.name} is {width}x{height}"
    assert colour == 6, "needs an alpha channel (RGBA, colour type 6)"
    assert depth == 8


def test_readme_credits_chadd_and_names_every_file():
    text = (ASSETS / "README.md").read_text(encoding="utf-8")
    assert "chadd" in text.lower()
    for mood in MOODS:
        assert f"ratchet_{mood}.png" in text
