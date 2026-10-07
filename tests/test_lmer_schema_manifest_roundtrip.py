"""``_plexus.yaml`` rewrites through ruamel.yaml's round trip (issue #365).

The manifest is hand-edited and annotated, and every PyYAML writer in lmer
drops every comment on rewrite. This file is the time-boxed spike that
decided the writer's backend — the decision and its reasoning are the
``## Manifest rewrites`` section of ``docs/DEVELOPMENT.md``; these tests are
its evidence and the contract the registry writer (#366) inherits:

- ``tests/fixtures/plexus_manifest/commented.yaml`` carries a comment in
  every position a rewrite has to survive (file header, above a top-level
  key, trailing a scalar, between ``projects:`` entries, inside a
  ``remotes:`` list, inside ``sources:`` and ``housekeeping:``), plus the
  blank lines, quoting styles and flow mappings SCHEMA.md's example uses;
- the three writes the registry writer needs — insert an entry
  (``repo``/``remotes``/``description``), remove an entry, canonicalize one
  ``repo:`` — each produce EXACTLY the intended lines and nothing else. The
  expected text is built by line surgery on the fixture, so a comment that
  moves one line fails the test even when a unified diff would still show
  only ``+``/``-`` lines;
- the ONE spelling ruamel does not preserve is pinned: ``~`` and an empty
  value are written back as ``null`` (the SCHEMA.md spelling; the value is
  unchanged);
- ``schema:`` is written back unchanged (never bumped to ``CURRENT_SCHEMA``)
  and a NEWER int — or the ``schema: true`` bool trap — is refused by the
  PyYAML read path BEFORE ruamel ever opens the file;
- the read path (``lmer_schema.manifest``, ``lmer_cli.container.sources``,
  ``lmer_cli.container.clone_and_exec``) never imports ruamel: it is
  checked on the modules' source and by importing them in a fresh process.

What the spike found, and what ``_insert_entry``/``_remove_entry`` below
exist to show: ruamel attaches everything that FOLLOWS a scalar — its
end-of-line comment, the blank lines after it and the full-line comments
above the next key — to that scalar's comment token, as one string. A
naive ``mapping[name] = entry`` therefore lands the new entry below the
comment that annotates its neighbour, and ``del mapping[name]`` takes the
next entry's comment (or the blank line before the next section) with it.
The writer has to move that token's remainder: on insert, everything after
the previous leaf's end-of-line comment goes to the new entry's last leaf;
on remove, the contiguous full-line comments directly above the removed key
are its own and go, and whatever followed its last leaf moves back to the
previous leaf. The helpers are the spike's measurement of that rule, not a
library — #366 owns the real implementation.
"""

import difflib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.tokens import CommentToken

from lmer_schema.manifest import (
    CURRENT_SCHEMA,
    SUPPORTED_SCHEMAS,
    ManifestError,
    parse_manifest,
)
from lmer_schema.urls import normalize

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "plexus_manifest" / "commented.yaml"

#: The modules that READ the manifest (or its ``sources:`` tier) and must
#: stay PyYAML-only: ``clone_and_exec`` runs standalone in the container
#: with a guarded PyYAML import and never writes the manifest.
READ_PATH_MODULES = (
    "lmer_schema.manifest",
    "lmer_cli.container.sources",
    "lmer_cli.container.clone_and_exec",
)

#: The entry every insert test adds — all three documented keys.
NEW_NAME = "gizmos"
NEW_ENTRY = {
    "repo": "git.example.com/acme/gizmos",
    "remotes": ["github.com/acme/gizmos"],
    "description": "Gizmos service",
}
NEW_ENTRY_LINES = [
    "  gizmos:\n",
    "    repo: git.example.com/acme/gizmos\n",
    "    remotes:\n",
    "      - github.com/acme/gizmos\n",
    "    description: Gizmos service\n",
]


# ---------------------------------------------------------------------------
# The round-trip configuration under test
# ---------------------------------------------------------------------------


class _Representer(RoundTripRepresenter):
    """ruamel writes ``None`` as an empty value by default; the manifest's
    documented spelling is ``null`` (SCHEMA.md). A subclass keeps the
    representer registration off the shared class."""


_Representer.add_representer(
    type(None),
    lambda representer, _data: representer.represent_scalar(
        "tag:yaml.org,2002:null", "null"
    ),
)


def _yaml():
    """The round-trip loader/dumper configured for the manifest's style."""
    rt = YAML(typ="rt")
    rt.Representer = _Representer
    # Keep '...' and "..." as written; without this ruamel drops quotes it
    # deems unnecessary.
    rt.preserve_quotes = True
    # SCHEMA.md style: two-space mappings, list items indented under their
    # key. ruamel's default (`- item` level with the key) would re-indent
    # every list in the file.
    rt.indent(mapping=2, sequence=4, offset=2)
    # Never fold a long `description:` onto a second line.
    rt.width = 4096
    return rt


def _dump(doc):
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _rewrite(text, mutate):
    """The write sequence the registry writer must follow: the PyYAML read
    path validates the document (schema int, bool trap, version floor, key
    charset) and only THEN does ruamel open it for the edit."""
    parse_manifest(text)
    doc = _yaml().load(text)
    mutate(doc)
    return _dump(doc)


def _fixture_text():
    return FIXTURE.read_text(encoding="utf-8")


def _fixture_lines():
    return _fixture_text().splitlines(keepends=True)


def _line_index(lines, needle):
    """The index of the ONE fixture line containing *needle*."""
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched lines {hits}; the fixture changed"
    return hits[0]


def _assert_text(expected, actual):
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(True), actual.splitlines(True),
            "expected", "ruamel", n=2,
        )
    )
    assert actual == expected, f"round trip differs from the intended text:\n{diff}"


def _entry(mapping):
    """A fresh ``projects:`` entry in document order."""
    entry = CommentedMap()
    for key, value in mapping.items():
        entry[key] = CommentedSeq(value) if isinstance(value, list) else value
    return entry


# ---------------------------------------------------------------------------
# Comment relocation — the rule the writer inherits (module docstring)
# ---------------------------------------------------------------------------


def _last_leaf(node):
    """``(container, key)`` of the last scalar under *node*: the slot ruamel
    hangs everything that follows the node on."""
    while True:
        if isinstance(node, CommentedMap) and len(node):
            key = list(node.keys())[-1]
        elif isinstance(node, CommentedSeq) and len(node):
            key = len(node) - 1
        else:
            raise TypeError("an entry with no keys has no leaf to carry comments")
        child = node[key]
        if isinstance(child, (CommentedMap, CommentedSeq)) and len(child):
            node = child
        else:
            return node, key


def _slot(container):
    # ruamel's ca.items layout: a mapping's post-value token sits at index
    # 2, a sequence item's at index 0.
    return 2 if isinstance(container, CommentedMap) else 0


def _get_token(container, key):
    item = container.ca.items.get(key)
    return item[_slot(container)] if item else None


def _set_token(container, key, token):
    item = container.ca.items.setdefault(key, [None, None, None, None])
    item[_slot(container)] = token


def _split_token(value):
    """``(eol, remainder)``: the end-of-line comment (or the bare line end)
    and the blank lines / full-line comments that follow it."""
    head, sep, rest = value.partition("\n")
    return head + sep, rest


def _drop_owned_comments(remainder):
    """Strip the contiguous full-line comments at the END of *remainder* —
    the ones directly above the next key, which annotate that key."""
    lines = remainder.splitlines(keepends=True)
    while lines and lines[-1].strip().startswith("#"):
        lines.pop()
    return "".join(lines)


def _preceding_slot(doc, section, index):
    """The slot whose token holds what sits above entry *index*: the previous
    entry's last leaf, or the section key itself for the first entry."""
    entries = doc[section]
    if index == 0:
        return doc, section
    return _last_leaf(entries[list(entries)[index - 1]])


def _insert_entry(doc, section, name, entry, index=None):
    entries = doc[section]
    if index is None:
        index = len(entries)
    container, key = _preceding_slot(doc, section, index)
    token = _get_token(container, key)
    entries.insert(index, name, entry)
    if token is None:
        return
    eol, remainder = _split_token(token.value)
    if remainder:
        # Mutated in place: ruamel can hold the same token object in two
        # slots (a section key's and its mapping's ``ca.comment``).
        token.value = eol
        leaf, leaf_key = _last_leaf(entry)
        _set_token(leaf, leaf_key, CommentToken("\n" + remainder, CommentMark(0), None))


def _remove_entry(doc, section, name):
    entries = doc[section]
    index = list(entries).index(name)
    container, key = _preceding_slot(doc, section, index)
    previous = _get_token(container, key)
    if previous is not None:
        eol, remainder = _split_token(previous.value)
        previous.value = eol + _drop_owned_comments(remainder)
    leaf, leaf_key = _last_leaf(entries[name])
    trailing = _get_token(leaf, leaf_key)
    del entries[name]
    if trailing is None:
        return
    _, remainder = _split_token(trailing.value)
    if not remainder:
        return
    if previous is not None:
        previous.value += remainder
    else:
        _set_token(container, key, CommentToken("\n" + remainder, CommentMark(0), None))


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_fixture_carries_every_comment_position():
    """Every position the issue lists, by name, so the fixture cannot
    quietly lose its purpose. Blank lines and both quote styles too."""
    lines = _fixture_lines()
    assert lines[0].startswith("#") and lines[1].startswith("#"), "file header"
    assert lines[2] == "\n", "blank line after the header"
    assert lines[_line_index(lines, "projects:") - 1].startswith("#"), (
        "full-line comment above a top-level key"
    )
    assert "schema: 1  #" in lines[_line_index(lines, "schema:")], "trailing a scalar"
    gadgets = _line_index(lines, "  gadgets:")
    assert lines[gadgets - 1].strip().startswith("#"), "between projects entries"
    remotes = _line_index(lines, "    remotes:")
    assert lines[remotes + 1].strip().startswith("#"), "inside a remotes list"
    assert "  # quoted on purpose" in lines[_line_index(lines, "'gitlab.com")], (
        "trailing a list item, single-quoted"
    )
    sources = _line_index(lines, "sources:")
    assert lines[sources + 1].strip().startswith("#"), "inside sources"
    housekeeping = _line_index(lines, "housekeeping:")
    assert lines[housekeeping - 1].startswith("#"), "above housekeeping"
    assert lines[_line_index(lines, "  plans:") - 1].strip().startswith("#"), (
        "inside housekeeping"
    )
    assert any('"' in line for line in lines), "a double-quoted scalar"
    assert any(": {" in line for line in lines), "a flow mapping (SCHEMA.md style)"
    assert sum(1 for line in lines if "#" in line) >= 7


def test_fixture_is_a_valid_manifest_for_the_read_path():
    manifest = parse_manifest(_fixture_text(), path=FIXTURE)
    assert manifest.schema == 1
    assert manifest.name == "acme"
    assert list(manifest.projects) == ["widgets", "gadgets", "capacity-planning"]
    assert manifest.warnings == ()


# ---------------------------------------------------------------------------
# (1) Comments, blank lines, quoting, indentation
# ---------------------------------------------------------------------------


def test_identity_round_trip_is_byte_for_byte():
    text = _fixture_text()
    _assert_text(text, _rewrite(text, lambda doc: None))


def test_insert_entry_appends_after_the_last_entry():
    """Append: the new entry goes directly under ``capacity-planning``; the
    blank line that separates ``projects:`` from ``sources:`` stays where
    it was instead of moving above the new entry."""
    lines = _fixture_lines()
    at = _line_index(lines, "    description:") + 1
    expected = "".join(lines[:at] + NEW_ENTRY_LINES + lines[at:])

    out = _rewrite(
        _fixture_text(),
        lambda doc: _insert_entry(doc, "projects", NEW_NAME, _entry(NEW_ENTRY)),
    )

    _assert_text(expected, out)
    assert yaml.safe_load(out)["projects"][NEW_NAME] == NEW_ENTRY


def test_insert_entry_between_entries_keeps_the_neighbours_comment():
    """Insert after ``widgets``: ``# gadgets moved to GitHub...`` annotates
    ``gadgets`` and must stay directly above it, below the new entry."""
    lines = _fixture_lines()
    at = _line_index(lines, "  # gadgets moved")
    expected = "".join(lines[:at] + NEW_ENTRY_LINES + lines[at:])

    out = _rewrite(
        _fixture_text(),
        lambda doc: _insert_entry(doc, "projects", NEW_NAME, _entry(NEW_ENTRY), index=1),
    )

    _assert_text(expected, out)
    assert list(yaml.safe_load(out)["projects"]) == [
        "widgets", NEW_NAME, "gadgets", "capacity-planning",
    ]


def test_insert_entry_first_keeps_the_section_header():
    lines = _fixture_lines()
    at = _line_index(lines, "projects:") + 1
    expected = "".join(lines[:at] + NEW_ENTRY_LINES + lines[at:])

    out = _rewrite(
        _fixture_text(),
        lambda doc: _insert_entry(doc, "projects", NEW_NAME, _entry(NEW_ENTRY), index=0),
    )

    _assert_text(expected, out)


@pytest.mark.parametrize(
    "name, first, last",
    [
        # The first entry: its trailing comment block belongs to `gadgets`
        # and must survive the removal.
        ("widgets", "  widgets:", "  # quoted on purpose"),
        # An entry with its own comment directly above it: that comment is
        # the entry's and goes with it.
        ("gadgets", "  # gadgets moved", "    repo: github.com/acme/gadgets"),
        # The last entry: the blank line before `sources:` stays.
        ("capacity-planning", "  capacity-planning:", "    description:"),
    ],
)
def test_remove_entry_takes_only_its_own_lines(name, first, last):
    lines = _fixture_lines()
    start = _line_index(lines, first)
    stop = _line_index(lines, last) + 1
    assert all(line.startswith("  ") for line in lines[start:stop])
    expected = "".join(lines[:start] + lines[stop:])

    out = _rewrite(_fixture_text(), lambda doc: _remove_entry(doc, "projects", name))

    _assert_text(expected, out)
    assert name not in yaml.safe_load(out)["projects"]


def test_canonicalize_repo_keeps_the_trailing_comment():
    """One scalar changes; its end-of-line comment stays on the line, at
    the column it had (ruamel pads the shorter value to keep it there)."""
    lines = _fixture_lines()
    at = _line_index(lines, "    repo: https://git.example.com/acme/widgets.git")
    original = lines[at]
    canonical = normalize("https://git.example.com/acme/widgets.git")
    assert canonical == "git.example.com/acme/widgets"
    column = original.index("#")
    value = f"    repo: {canonical}"
    assert len(value) < column
    lines[at] = value.ljust(column) + original[column:]
    expected = "".join(lines)

    def canonicalize(doc):
        doc["projects"]["widgets"]["repo"] = normalize(doc["projects"]["widgets"]["repo"])

    out = _rewrite(_fixture_text(), canonicalize)

    _assert_text(expected, out)
    assert yaml.safe_load(out)["projects"]["widgets"]["repo"] == canonical


def test_value_past_the_comment_column_pushes_the_comment_one_space():
    """The other half of the column rule: a value longer than the comment's
    column keeps the comment on the line, one space after the value."""
    lines = _fixture_lines()
    at = _line_index(lines, "    repo: https://git.example.com/acme/widgets.git")
    original = lines[at]
    column = original.index("#")
    longer = "git.example.com/acme/a-name-that-runs-past-the-comment-column"
    value = f"    repo: {longer}"
    assert len(value) > column
    lines[at] = f"{value} {original[column:]}"
    expected = "".join(lines)

    def rewrite(doc):
        doc["projects"]["widgets"]["repo"] = longer

    _assert_text(expected, _rewrite(_fixture_text(), rewrite))


def test_null_spelling_is_the_one_normalization():
    """The documented loss: ``~`` and an empty value come back as ``null``
    (SCHEMA.md's spelling). The value is unchanged; nothing else moves —
    the longer spelling runs past the ``# never`` comment's column, so that
    comment follows one space after it (the column rule above)."""
    text = (
        "schema: 2\n"
        "name: edge\n"
        "housekeeping:\n"
        "  plans: {archive_after: ~}  # never\n"
        "  napkin:\n"
        "    archive_after:\n"
        "  # kept\n"
        "  specs:\n"
        "    archive_after: null\n"
    )
    expected = text.replace(
        "{archive_after: ~}  # never", "{archive_after: null} # never"
    ).replace("archive_after:\n", "archive_after: null\n")
    out = _rewrite(text, lambda doc: None)
    _assert_text(expected, out)
    assert yaml.safe_load(out)["housekeeping"] == yaml.safe_load(text)["housekeeping"]


def test_long_values_and_flow_styles_are_not_reflowed():
    """Width is unlimited and flow mappings/sequences stay flow: a long
    ``description:`` is not folded and ``{}``/``[]`` are not exploded."""
    long_description = "a description that runs well past eighty columns " * 3
    text = (
        "schema: 2\n"
        "name: edge\n"
        "projects:  # on the section key\n"
        "  # above the first entry\n"
        "  one:\n"
        f"    description: {long_description.strip()}\n"
        "  two:\n"
        "    repo: github.com/x/two\n"
        "    remotes: []\n"
        "  three: {}\n"
        "sources:\n"
        '  napkin: {repo: "https://h/n.git"}  # trailing a flow map\n'
    )
    _assert_text(text, _rewrite(text, lambda doc: None))


# ---------------------------------------------------------------------------
# (2) The schema int: written back unchanged, a newer int refused first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", SUPPORTED_SCHEMAS)
def test_schema_int_is_written_back_unchanged(schema):
    """A rewrite never bumps ``schema:`` — that is a migration's job — and
    the int stays a plain int for the PyYAML read path."""
    lines = _fixture_lines()
    at = _line_index(lines, "schema:")
    lines[at] = lines[at].replace("schema: 1", f"schema: {schema}")
    text = "".join(lines)

    out = _rewrite(
        text, lambda doc: _insert_entry(doc, "projects", NEW_NAME, _entry(NEW_ENTRY))
    )

    assert f"schema: {schema}  # an int, never a bool\n" in out
    assert type(yaml.safe_load(out)["schema"]) is int
    assert yaml.safe_load(out)["schema"] == schema
    assert parse_manifest(out).schema == schema


@pytest.mark.parametrize(
    "value, fragment",
    [
        (str(CURRENT_SCHEMA + 1), "pgrade lmer"),
        ("true", "integer `schema:`"),
    ],
)
def test_newer_or_bool_schema_is_refused_before_any_write(tmp_path, value, fragment):
    """The PyYAML read path is the gate: a newer int (§8 version floor) or
    the ``schema: true`` bool trap raises ``ManifestError`` and ruamel is
    never reached, so the file on disk is untouched byte for byte."""
    path = tmp_path / "_plexus.yaml"
    text = _fixture_text().replace("schema: 1", f"schema: {value}", 1)
    path.write_bytes(text.encode("utf-8"))
    opened = []

    def mutate(doc):
        opened.append(doc)

    with pytest.raises(ManifestError) as excinfo:
        out = _rewrite(path.read_text(encoding="utf-8"), mutate)
        path.write_text(out, encoding="utf-8")

    assert fragment in str(excinfo.value)
    assert opened == []
    assert path.read_bytes() == text.encode("utf-8")


# ---------------------------------------------------------------------------
# The read path stays PyYAML-only
# ---------------------------------------------------------------------------


def test_read_path_sources_never_mention_ruamel():
    for module in READ_PATH_MODULES:
        source = (SRC_ROOT / Path(*module.split("."))).with_suffix(".py")
        assert "ruamel" not in source.read_text(encoding="utf-8"), module


def test_read_path_imports_do_not_load_ruamel():
    """Importing every read-side module in a fresh interpreter loads no
    ``ruamel*`` module — a transitive import would show up here even when
    the module's own source is clean."""
    script = (
        "import sys\n"
        + "".join(f"import {module}\n" for module in READ_PATH_MODULES)
        + "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'ruamel')\n"
        "assert not loaded, loaded\n"
        "assert 'yaml' in sys.modules\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
