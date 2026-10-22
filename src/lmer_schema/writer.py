"""``_lmer.yaml`` rewrites — the ONE tool-side write path (issue #366).

Spec D10 says the registry is tool-written on adoption and §4 that the tool
writer always emits canonical form. This module is that writer: an importable
API with no CLI, shared by ``work adopt`` (#367) and ``work migrate`` (#368)
so neither grows YAML dump code of its own and the things the validator
polices — canonical remote spelling, key order, the schema-int refusal rule,
comment handling — have exactly one implementation.

The API
-------
- :func:`load_for_write` opens a manifest for mutation and returns a
  :class:`ManifestWriter` — the document handle, with the parsed model on
  ``.manifest``. It REFUSES (raises) a ``schema:`` newer than this lmer
  knows (§4 "tools refuse to write"), an unparseable file, and a manifest
  whose registry already fails validation: the writer never makes a broken
  manifest worse. It also refuses a MISSING file — :func:`create` is the
  explicit bootstrap and is never implied by a mutation, because a present
  manifest is what flips a tree from legacy to schema form (D20).
- :meth:`ManifestWriter.add_project`, :meth:`~ManifestWriter.remove_project`,
  :meth:`~ManifestWriter.rename_project`, :meth:`~ManifestWriter.set_sources`
  and :meth:`~ManifestWriter.canonicalize_project` (the third write the
  #365 spike measured) each validate, write atomically and refresh
  ``.manifest`` from what was written. Each returns whether the file was
  rewritten (a mutation that changes nothing writes nothing).
- :func:`create` writes a minimal ``schema: 1`` manifest for a tree that has
  none and returns the handle, so adoption is two explicit calls on one
  handle: bootstrap, then register.

Registry remotes vs source transports
=====================================
A ``projects.<name>.repo``/``remotes:`` value is an IDENTITY: every one
passes through the one normalizer — :func:`lmer_schema.urls.normalize` over
``normalize_repo_url`` (D21) — before it is written, so those fields only
ever hold canonical ``host/path`` (a credential in the input is stripped by
normalization, never written). A ``sources.<key>.repo`` is a TRANSPORT the
container clones from (run-identity spec §13.B, astra PR1): ``set_sources``
stores each entry's spelling and ``ref`` VERBATIM, applies no identity
normalization, and refuses only an entry carrying an embedded credential or
an empty ``repo``. Trust validation of a source entry (the same-host rule,
``ref`` under ``taskdef`` only) is the caller's, through
``lmer_cli.container.sources.validate_source_entry``; it is not restated
here.

Pre-write invariants are the registry's own
===========================================
No registry rule is restated. After every mutation the document is dumped,
re-parsed with :func:`lmer_schema.manifest.parse_manifest` and rebuilt with
:func:`lmer_schema.registry.build_registry` — the short-name charset,
``remotes:`` without ``repo:``, ``repo:`` repeated in ``remotes:``, two
values folding to one key, one remote bound to two projects, a value with no
host — and only a document that passes reaches disk. A refused mutation
raises the loader's own ``ManifestError``/``RegistryError`` with the file
untouched, and the handle rolls its document back so it stays usable.
:class:`WriterError` (a ``ManifestError``) is reserved for the writer's own
refusals: an unknown or duplicate project name, a malformed or credentialed
source entry, a source entry assembled with a YAML merge key, a missing or
pre-existing file, a file changed on disk since it was loaded, a failed
write.

The write
=========
Atomic: a temp file in the manifest's directory, fsynced, mode preserved,
then ``os.replace``. A failure anywhere leaves the original byte for byte
and no temp file behind. The write sequence is the one the #365 spike fixed
(``docs/DEVELOPMENT.md``, "Manifest rewrites"): ``parse_manifest`` FIRST —
the PyYAML read path is the gate, so a newer ``schema:`` or the
``schema: true`` bool trap is refused before ruamel ever opens the document
— and ``schema:`` is written back unchanged, never bumped (that is a
migration's job, :mod:`lmer_schema.migration`).

Comments — ruamel.yaml round trip and the relocation rule
=========================================================
Backend: ruamel.yaml round-trip mode with the spike's configuration
(``preserve_quotes``, ``indent(mapping=2, sequence=4, offset=2)``,
``width=4096``, ``None`` written as ``null`` on a private representer
subclass). ruamel is imported LAZILY, inside :func:`_ruamel`: the package
root re-exports this module eagerly, and the read path (``work`` commands,
``clone_and_exec``) must never pay for a YAML library it does not use —
``tests/test_lmer_schema_manifest_roundtrip.py`` and the writer's own tests
pin that in a fresh interpreter.

What the spike measured: ruamel hangs everything that FOLLOWS a scalar —
its end-of-line comment, the blank lines after it and the full-line
comments above the next key — on that scalar's comment token, as one
string. A naive insert lands the new entry below the comment that
annotates its neighbour; a naive delete takes the next entry's comment, or
the blank line before the next section, with it. The rule, ported from the
spike's ``_insert_entry``/``_remove_entry`` (never imported from the test):

- on insert, everything after the previous leaf's end-of-line comment moves
  to the new entry's last leaf;
- on remove, the contiguous full-line comments directly above the removed
  key are its own and go with it; whatever followed its last leaf moves
  back to the previous leaf — the section key's own slot when the first
  entry goes. Blank lines are never dropped.

Two things the spike left to this module:

1. ruamel does not always use that one placement. After a value with no
   scalar token to hang on — an empty ``{}``/``[]`` or an empty ``key:`` —
   the following lines land on the NEXT key's pre-comment list (which may
   sit in an ancestor mapping), and the comments between a key line and its
   first block child land on that key's fourth slot. :func:`_fold_comments`
   runs once on load and folds both placements into the post-value token
   of the preceding leaf, so the rule above has ONE representation to work
   on. One token in those lists is not a full line: a key line's
   end-of-line comment, which ruamel parks there when an anchor or alias
   sits between the key and the comment (``taskdef: &common  # note``);
   :func:`_fold_eol` recognizes it by its source line and puts it back on
   that key's slot, column kept. The fold is dump-neutral: an unchanged
   document still round-trips byte for byte.
2. **An entry with no keys** (``name: {}`` — a D19 project registered by
   name alone — or a hand-written ``name:``) has no inner leaf. Decision:
   the entry's own key slot IS its leaf. :func:`_last_leaf` descends only
   into NON-EMPTY BLOCK collections, so an empty value, a ``null`` and a
   flow mapping (``{repo: ...}``, whose trailing comment ruamel hangs on
   the key, not inside) all resolve to the key itself, and ruamel's emitter
   honours a post-value token on that slot. The same decision makes the
   writer's own ``name: {}`` output carry what follows it.

Known, accepted normalizations (the spike's): ``~`` and an empty value are
written back as ``null``; an end-of-line comment keeps its column (one
space after a value that runs past it); list indentation is the tool's
style. An existing scalar is reassigned only when its value changes, so a
``'quoted on purpose'`` remote keeps its quotes.

``set_sources`` is a merge, not a replacement: an entry already in the
section is updated scalar by scalar in place (its comments survive), a new
one is appended, one absent from the mapping is removed with its own
comments, and the section's existing key order wins over the mapping's.
Setting the section to exactly what it holds is a no-op.

Anchors and aliases — one slot, one node
========================================
YAML lets one node answer to two keys — ``taskdef: &common {repo: ...}``
and ``napkin: *common`` — and ruamel's round trip keeps that as ONE
Python object reachable from both. Every in-place update above would then
reach the other key too: ``set_sources`` moving ``taskdef`` to a new
``repo`` while ``napkin`` keeps the old one mutated the shared mapping
twice, the second update undid the first, and the call returned ``True``
with BOTH sources persisted at the old value (MR !380 review of this
module). The comment fold had the same blind spot: it walked the shared
node once per key and :func:`_last_leaf` descended into it through the
alias, so the lines after ``napkin: *common`` were relocated under
``taskdef``.

Decision: DETACH, never refuse — a configuration change must take effect.
:func:`_load` runs :func:`_detach_aliases` before the fold: the first slot
in document order keeps the node; every later slot that reaches the same
mapping or sequence gets its own comment-free deep copy (same values, same
flow/block style; the key-line comment ruamel mirrors onto the alias key
is dropped there so it prints once). From then on one slot is one node,
for the fold, the relocation rule and every mutation alike. The emitted
document therefore carries INDEPENDENT mappings: the ``&anchor``/``*alias``
syntax is dropped for those entries the first time anything is written
(ruamel writes an anchor only for a node still referenced twice). A
mutation that changes nothing still writes nothing, so an untouched file
keeps its aliases; a document that shares nothing is not affected at all
(the round trips above stay byte for byte). Anchored SCALARS are left
alone: a scalar is reassigned, never mutated, so sharing one cannot leak
an update.

Merge keys are the other way a document shares an entry — ``napkin: {<<:
*common}`` — and the one this module REFUSES: ``napkin`` is its own node
whose ``repo`` READS THROUGH to ``taskdef``'s, so an update written key by
key cannot keep the two apart (the probe behind this section left
``napkin`` reading ``taskdef``'s new value, again with ``True`` returned).
``set_sources`` raises :class:`WriterError` naming the merged entries
before any write, unless the mapping is exactly what the section already
holds (still a no-op). A ``projects:`` entry can only merge what the
registry lets two entries share — a ``description`` — which no mutation
rewrites in place, so no rule is needed there.
"""

from __future__ import annotations

import copy
import io
import os
import tempfile
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from lmer_cli.container.sources import scrub_credentials, url_has_embedded_credential
from lmer_schema.manifest import (
    MANIFEST_NAME,
    TOP_LEVEL_KEYS,
    Manifest,
    ManifestError,
    parse_manifest,
)
from lmer_schema.registry import build_registry
from lmer_schema.urls import normalize

__all__ = ["ManifestWriter", "WriterError", "create", "load_for_write"]

#: The ``schema:`` int :func:`create` writes. Adoption of a legacy tree lands
#: on schema 1 (spec §13.B: the legacy→schema-1 nesting of #368); the 1→2
#: cutover is :func:`lmer_schema.migration.migrate`'s, with its blockers —
#: a bootstrap never skips it.
BOOTSTRAP_SCHEMA = 1

#: The ``projects:`` entry keys, in the order the writer emits them
#: (SCHEMA.md registry table).
_PROJECT_KEYS = ("repo", "remotes", "description")


class WriterError(ManifestError):
    """A write the writer itself refuses (module docstring, "Pre-write
    invariants"). A :class:`~lmer_schema.manifest.ManifestError`, so a
    consumer that already treats one as "refused" needs no second clause."""


# ---------------------------------------------------------------------------
# ruamel.yaml — imported on first use, configured the spike's way
# ---------------------------------------------------------------------------

_LIB: Optional[types.SimpleNamespace] = None


def _ruamel() -> types.SimpleNamespace:
    """The ruamel.yaml names this module uses, imported on first call.

    Module-scope would be simpler, but the package root re-exports this
    module eagerly and ``import lmer_schema`` must stay PyYAML-only (module
    docstring). The representer subclass is built here too, so the ``null``
    registration never touches ruamel's shared class.
    """
    global _LIB
    # deferred: import cost — ruamel (~20 ms) must stay off the PyYAML-only read path; lmer_schema re-exports this module eagerly
    from ruamel.yaml import YAML
    from ruamel.yaml.anchor import Anchor
    from ruamel.yaml.comments import Comment, CommentedMap, CommentedSeq, merge_attrib
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.representer import RoundTripRepresenter
    from ruamel.yaml.tokens import CommentToken

    if _LIB is None:
        class _Representer(RoundTripRepresenter):
            """ruamel writes ``None`` as an empty value by default; the
            manifest's documented spelling is ``null`` (SCHEMA.md)."""

        _Representer.add_representer(
            type(None),
            lambda representer, _data: representer.represent_scalar(
                "tag:yaml.org,2002:null", "null"
            ),
        )
        _LIB = types.SimpleNamespace(
            YAML=YAML,
            Map=CommentedMap,
            Seq=CommentedSeq,
            Mark=CommentMark,
            Token=CommentToken,
            Representer=_Representer,
            # The instance attributes behind a node's lazy ``.ca``, ``.anchor``
            # and merge list: deleting one resets it (``_detached_copy``).
            CommentAttrib=Comment.attrib,
            AnchorAttrib=Anchor.attrib,
            MergeAttrib=merge_attrib,
        )
    return _LIB


def _yaml():
    """The round-trip loader/dumper, configured for the manifest's style."""
    lib = _ruamel()
    rt = lib.YAML(typ="rt")
    rt.Representer = lib.Representer
    # Keep '...' and "..." as written; without this ruamel drops quotes it
    # deems unnecessary.
    rt.preserve_quotes = True
    # SCHEMA.md style: two-space mappings, list items indented under their
    # key.
    rt.indent(mapping=2, sequence=4, offset=2)
    # Never fold a long `description:` onto a second line.
    rt.width = 4096
    return rt


def _load(text: str):
    """*text* as a round-trip document with every alias given its own node
    and its comments folded into the one placement the relocation rule
    works on (module docstring). Detach FIRST: the fold must walk a tree."""
    doc = _yaml().load(text)
    if not isinstance(doc, _ruamel().Map):  # parse_manifest already refused it
        raise TypeError(f"{MANIFEST_NAME} is not a mapping document")
    _detach_aliases(doc)
    _fold_comments(None, doc)
    return doc


def _dump(doc) -> str:
    buffer = io.StringIO()
    _yaml().dump(doc, buffer)
    return buffer.getvalue()


def _to_node(value):
    """A plain mapping/list as ruamel nodes (block style), scalars as is."""
    lib = _ruamel()
    if isinstance(value, Mapping):
        node = lib.Map()
        for key, item in value.items():
            node[key] = _to_node(item)
        return node
    if isinstance(value, (list, tuple)):
        return lib.Seq(_to_node(item) for item in value)
    return value


# ---------------------------------------------------------------------------
# Anchors and aliases — one slot, one node
# ---------------------------------------------------------------------------


def _has_merge(node) -> bool:
    """*node* is a mapping assembled with a YAML merge key (``<<: *x``)."""
    lib = _ruamel()
    return isinstance(node, lib.Map) and bool(getattr(node, lib.MergeAttrib, None))


def _own_keys(node) -> list:
    """The keys *node* holds itself. A merged key reads through to another
    mapping's node, which is that mapping's slot to keep or detach."""
    if _has_merge(node):
        return [key for key, _ in node.non_merged_items()]
    return _keys(node)


def _detached_copy(node):
    """A deep copy of *node* carrying its values and flow/block style and
    nothing the first slot already prints: no comments, no anchor."""
    lib = _ruamel()
    duplicate = copy.deepcopy(node)
    pending = [duplicate]
    while pending:
        current = pending.pop()
        for attrib in (lib.CommentAttrib, lib.AnchorAttrib):
            if hasattr(current, attrib):
                delattr(current, attrib)
        pending.extend(
            child for child in (current[key] for key in _own_keys(current)) if _is_collection(child)
        )
    return duplicate


def _drop_mirrored_tokens(anchor_slot, alias_slot) -> None:
    """ruamel records a block mapping's key-line comment on the anchor's key
    slot AND on every alias's, as one list object. Detached, each slot
    prints its own, so the alias's copy of the shared token goes."""
    anchor = anchor_slot[0].ca.items.get(anchor_slot[1])
    alias = alias_slot[0].ca.items.get(alias_slot[1])
    if not anchor or not alias:
        return
    if alias is anchor:
        alias_slot[0].ca.items[alias_slot[1]] = [None] * len(anchor)
        return
    for index, token in enumerate(alias):
        if token is not None and index < len(anchor) and anchor[index] is token:
            alias[index] = None


def _detach_aliases(node, first: Optional[dict] = None) -> None:
    """Give every alias its own node (module docstring, "Anchors and
    aliases"). Walks *node* in document order: the first slot that reaches
    a mapping or sequence keeps it; every later slot reaching the SAME
    object gets a :func:`_detached_copy` in its place. Scalars, and the
    keys a mapping only merges in, are not touched."""
    if first is None:
        first = {}
    for key in _own_keys(node):
        child = node[key]
        if not _is_collection(child):
            continue
        anchor_slot = first.get(id(child))
        if anchor_slot is None:
            first[id(child)] = (node, key)
        else:
            child = _detached_copy(child)
            node[key] = child
            _drop_mirrored_tokens(anchor_slot, (node, key))
        _detach_aliases(child, first)


# ---------------------------------------------------------------------------
# Comment tokens — one representation, and the relocation rule
# ---------------------------------------------------------------------------
#
# A *slot* is ``(container, key)``: a mapping and one of its keys, or a
# sequence and an index. Its post-value token (``ca.items[key][2]`` for a
# mapping, ``[0]`` for a sequence) holds the end-of-line comment on that
# line, and after the first newline everything that followed the line —
# blank lines and full-line comments — up to the next key.


def _is_collection(node) -> bool:
    lib = _ruamel()
    return isinstance(node, (lib.Map, lib.Seq))


def _is_block(node) -> bool:
    """A non-empty block-style collection: the only kind :func:`_last_leaf`
    descends into (module docstring, decision 2)."""
    return _is_collection(node) and len(node) > 0 and node.fa.flow_style() is not True


def _keys(node) -> list:
    return list(node.keys()) if isinstance(node, _ruamel().Map) else list(range(len(node)))


def _post_index(container) -> int:
    return 2 if isinstance(container, _ruamel().Map) else 0


def _post_token(slot):
    container, key = slot
    item = container.ca.items.get(key)
    return item[_post_index(container)] if item else None


def _set_post_token(slot, value: str) -> None:
    container, key = slot
    lib = _ruamel()
    item = container.ca.items.setdefault(key, [None, None, None, None])
    item[_post_index(container)] = lib.Token(value, lib.Mark(0), None)


def _append_remainder(slot, lines: str) -> None:
    """Hang *lines* (full lines, indentation included) after *slot*'s line."""
    token = _post_token(slot)
    if token is None:
        _set_post_token(slot, "\n" + lines)
    else:
        token.value += lines


def _split(value: str) -> Tuple[str, str]:
    """``(eol, remainder)``: the end-of-line comment (or the bare line end)
    and the blank lines / full-line comments that follow it."""
    head, sep, rest = value.partition("\n")
    return head + sep, rest


def _drop_owned_comments(remainder: str) -> str:
    """Strip the contiguous full-line comments at the END of *remainder* —
    the ones directly above the next key, which annotate that key."""
    lines = remainder.splitlines(keepends=True)
    while lines and lines[-1].strip().startswith("#"):
        lines.pop()
    return "".join(lines)


def _last_leaf(container, key):
    """The slot ruamel hangs everything that follows ``container[key]`` on:
    the last scalar under it — or the key itself when its value is empty,
    ``null`` or a flow collection (module docstring, decision 2)."""
    while True:
        child = container[key]
        if _is_block(child):
            container, key = child, _keys(child)[-1]
        else:
            return container, key


def _preceding_slot(parent_slot, container, index):
    """The slot whose token holds what sits above entry *index* of
    *container*: the previous entry's last leaf, or the container's own key
    in its parent for the first entry (``None`` at the document root)."""
    if index == 0:
        return parent_slot
    return _last_leaf(container, _keys(container)[index - 1])


def _token_lines(tokens) -> str:
    """Pre-comment tokens as the full lines they print as: ruamel positions
    the first line of such a token by its column mark; any further lines
    in one token already carry their indentation."""
    if not isinstance(tokens, list):
        tokens = [tokens]
    return "".join(
        " " * token.start_mark.column + token.value for token in tokens if token is not None
    )


def _key_line(node, key) -> Optional[int]:
    """The 0-based source line *key* was read from; ``None`` for a key with
    no position (a node built by the writer, not loaded)."""
    try:
        return (node.lc.key(key) if isinstance(node, _ruamel().Map) else node.lc.item(key))[0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return None


def _eol_slot(container, key, line):
    """The slot on the :func:`_last_leaf` path from ``container[key]`` whose
    key was read from *line*, or ``None``: a comment token that starts on a
    key's own line is that key's end-of-line comment."""
    while True:
        if _key_line(container, key) == line:
            return container, key
        child = container[key]
        if not _is_block(child):
            return None
        container, key = child, _keys(child)[-1]


def _fold_eol(container, key, tokens: list) -> list:
    """Peel a leading token that starts on the line of a key on the path
    below ``container[key]`` onto that key's own post-value slot, column
    kept. ruamel parks a key line's end-of-line comment in another slot
    when an anchor or alias sits between the key and the comment; folded
    as full lines it would print on a line of its own. What followed the
    comment on later lines goes where the rule puts it. Returns the tokens
    left for the caller."""
    if not tokens or tokens[0] is None:
        return tokens
    slot = _eol_slot(container, key, tokens[0].start_mark.line)
    if slot is None or _post_token(slot) is not None:
        return tokens
    token = tokens[0]
    eol, remainder = _split(token.value)
    token.value = eol
    slot[0].ca.items.setdefault(slot[1], [None, None, None, None])[_post_index(slot[0])] = token
    if remainder:
        _append_remainder(_last_leaf(*slot), remainder)
    return tokens[1:]


def _as_tokens(value) -> list:
    return value if isinstance(value, list) else [value]


def _fold_comments(parent_slot, node) -> None:
    """Fold ruamel's two other comment placements into post-value tokens
    (module docstring, "Two things the spike left to this module", 1).

    Walks *node* in document order. A key's pre-comment list (slot 1) moves
    to the preceding leaf's token — the previous key's last leaf, or the
    parent slot for the first key. The comments between a key line and its
    first block child (slot 3, mirrored on the child's ``ca.comment``) move
    to that key's own token. In both lists a token that starts on a key's
    own line is that key's end-of-line comment and goes back onto its slot
    (:func:`_fold_eol`). Dump-neutral by construction: the lines come out
    exactly where they went in.
    """
    keys = _keys(node)
    for index, key in enumerate(keys):
        item = node.ca.items.get(key)
        if item is not None and item[1]:
            target = _preceding_slot(parent_slot, node, index)
            if target is not None:
                tokens, item[1] = _as_tokens(item[1]), None
                if index > 0:
                    tokens = _fold_eol(node, keys[index - 1], tokens)
                lines = _token_lines(tokens)
                if lines:
                    _append_remainder(target, lines)
        child = node[key]
        if item is not None and len(item) > 3 and item[3]:
            tokens, item[3] = _as_tokens(item[3]), None
            if _is_collection(child):
                child.ca.comment = None
            lines = _token_lines(_fold_eol(node, key, tokens))
            if lines:
                _append_remainder((node, key), lines)
        if _is_block(child):
            _fold_comments((node, key), child)


def _insert(parent_slot, container, index, key, value) -> None:
    """Insert ``key: value`` at *index*, moving what followed the previous
    leaf's line to the new value's last leaf (the spike's insert rule)."""
    slot = _preceding_slot(parent_slot, container, index)
    token = _post_token(slot) if slot is not None else None
    container.insert(index, key, value)
    if token is None:
        return
    eol, remainder = _split(token.value)
    if remainder:
        # Mutated in place: ruamel can hold one token object in two slots
        # (a section key's and its mapping's ``ca.comment``).
        token.value = eol
        _set_post_token(_last_leaf(container, key), "\n" + remainder)


def _remove(parent_slot, container, key) -> None:
    """Delete *key*, taking only its own lines: the full-line comments
    directly above it go with it; what followed its last leaf moves back
    to the previous leaf (the spike's remove rule)."""
    index = _keys(container).index(key)
    slot = _preceding_slot(parent_slot, container, index)
    previous = _post_token(slot) if slot is not None else None
    if previous is not None:
        eol, remainder = _split(previous.value)
        previous.value = eol + _drop_owned_comments(remainder)
    trailing = _post_token(_last_leaf(container, key))
    del container[key]
    # ``del`` leaves the key's comment slots behind; they are the entry's.
    container.ca.items.pop(key, None)
    if trailing is None:
        return
    _, remainder = _split(trailing.value)
    if not remainder:
        return
    if previous is not None:
        previous.value += remainder
    elif slot is not None:
        _set_post_token(slot, "\n" + remainder)


def _tidy_emptied(section) -> None:
    """A block-style mapping that a removal left EMPTY must be written as
    ``{}`` on the key's line: ruamel would otherwise put the ``{}`` on a
    line of its own below the key's end-of-line comment, which no YAML
    parser reads. :meth:`ManifestWriter._section` flips it back to block
    style the moment it gets an entry again."""
    if len(section) == 0:
        section.fa.set_flow_style()


def _rename(container, old, new) -> None:
    """Rename *old* to *new* in place: same position, same comment slots."""
    index = _keys(container).index(old)
    value = container[old]
    container.insert(index, new, value)
    del container[old]
    item = container.ca.items.pop(old, None)
    if item is not None:
        container.ca.items[new] = item


def _replace_value(container, key, value) -> None:
    """``container[key] = value``, keeping what followed the old value after
    the new one (a scalar's end-of-line comment stays on its line)."""
    source = _last_leaf(container, key)
    trailing = _post_token(source)
    container[key] = value
    target = _last_leaf(container, key)
    if trailing is None or target == source:
        return
    eol, remainder = _split(trailing.value)
    if remainder:
        trailing.value = eol
        _append_remainder(target, remainder)


# ---------------------------------------------------------------------------
# The atomic write
# ---------------------------------------------------------------------------


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _write_atomic(path: Path, text: str, mode: int) -> None:
    """Temp file beside *path*, fsynced, chmod *mode*, then ``os.replace``.
    Any failure leaves *path* untouched and the temp file removed."""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{MANIFEST_NAME}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise WriterError(path, f"cannot write {MANIFEST_NAME}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        raise WriterError(path, f"cannot write {MANIFEST_NAME}: {exc}") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------


class ManifestWriter:
    """An open ``_lmer.yaml``: the round-trip document plus the parsed
    model. Obtained from :func:`load_for_write` or :func:`create`, never
    built by hand.

    ``path`` is the manifest file; ``manifest`` the
    :class:`~lmer_schema.manifest.Manifest` as last written (refreshed after
    every mutation). Every mutation validates, writes atomically and
    returns whether the file was rewritten; a refused one raises with the
    file untouched and the document rolled back.
    """

    def __init__(self, path: Path, text: str, manifest: Manifest):
        self.path = Path(path)
        self.manifest = manifest
        self._text = text
        self._doc = _load(text)

    # -- mutations ----------------------------------------------------------

    def add_project(
        self,
        name: str,
        *,
        repo: Optional[str] = None,
        remotes: Iterable[str] = (),
        description: Optional[str] = None,
    ) -> bool:
        """Append a registry entry. ``repo`` and each of ``remotes`` are
        written in canonical form (D21); an empty ``remotes`` writes no
        ``remotes:`` key, and an entry with nothing at all is ``name: {}``
        (D19, by name alone). Refuses a name already registered; every
        registry rule raises through the registry itself."""
        if isinstance(remotes, (str, bytes)):
            raise WriterError(
                self.path,
                f"`projects.{name}.remotes` must be an iterable of remote URLs, "
                "not one string — pass `remotes=(url,)`.",
            )
        remotes = tuple(remotes)
        entries = self._section("projects", create=False)
        if entries is not None and name in entries:
            raise WriterError(
                self.path,
                f"`projects.{name}` already exists; `rename_project`/"
                "`remove_project` change an existing entry, `add_project` never "
                "overwrites one.",
            )
        where = f"projects.{name}"
        entry = _to_node({})
        if repo is not None:
            entry["repo"] = self._canonical(f"{where}.repo", repo)
        if remotes:
            entry["remotes"] = _to_node(
                [self._canonical(f"{where}.remotes:[{i}]", value) for i, value in enumerate(remotes)]
            )
        if description is not None:
            entry["description"] = description

        def mutate():
            entries = self._section("projects")
            _insert((self._doc, "projects"), entries, len(entries), name, entry)

        return self._apply(mutate)

    def remove_project(self, name: str) -> bool:
        """Delete a registry entry with only its own lines (module
        docstring, the remove rule). Refuses an unknown name."""
        self._require_project(name)

        def mutate():
            entries = self._doc["projects"]
            _remove((self._doc, "projects"), entries, name)
            _tidy_emptied(entries)

        return self._apply(mutate)

    def rename_project(self, old: str, new: str) -> bool:
        """Rename an entry in place — same position, same comments, same
        remotes. Refuses an unknown ``old`` or an already registered
        ``new``; the short-name charset of ``new`` raises through the
        loader like every other registry rule."""
        entries = self._require_project(old)
        if new in entries:
            raise WriterError(
                self.path,
                f"`projects.{new}` already exists; a rename never merges two entries.",
            )

        def mutate():
            _rename(self._doc["projects"], old, new)

        return self._apply(mutate)

    def set_sources(self, mapping: Mapping) -> bool:
        """Make ``sources:`` hold exactly *mapping* (``name -> {repo, ref?}``).

        Each entry's ``repo`` is a transport and is written VERBATIM, as is
        ``ref`` and anything else in the entry; the registry's identity
        normalizer is never applied (module docstring). Refuses an entry
        that is not a mapping, has no non-empty string ``repo``, or embeds a
        credential (never echoed). Trust rules are the caller's.

        An entry aliased to another (``napkin: *common``) is written out
        as its own mapping, alias syntax dropped; one assembled with a
        merge key (``napkin: {<<: *common}``) is refused unless *mapping*
        is exactly what the section holds (module docstring, "Anchors and
        aliases").
        """
        entries = self._check_sources(mapping)

        def mutate():
            section = self._section("sources", create=bool(entries))
            if section is None:
                return False
            self._refuse_merged(section, entries)
            section_slot = (self._doc, "sources")
            changed = False
            for key in _keys(section):
                if key not in entries:
                    _remove(section_slot, section, key)
                    changed = True
            for key, entry in entries.items():
                if key in section:
                    changed = self._update_entry(section_slot, section, key, entry) or changed
                else:
                    _insert(section_slot, section, len(section), key, _to_node(entry))
                    changed = True
            _tidy_emptied(section)
            return changed

        return self._apply(mutate)

    def canonicalize_project(self, name: str) -> bool:
        """Rewrite the entry's ``repo:`` and ``remotes:`` in canonical form,
        in place (the spike's third write: each scalar keeps its line and
        its end-of-line comment). What the ``registry.non-canonical-remote``
        WARN means by "tools write this form". A no-op when already
        canonical; refuses an unknown name."""
        entries = self._require_project(name)

        def mutate():
            entry = entries[name]
            if not isinstance(entry, _ruamel().Map):
                return False
            changed = False
            repo = entry.get("repo")
            if isinstance(repo, str) and normalize(repo) != repo:
                entry["repo"] = normalize(repo)
                changed = True
            remotes = entry.get("remotes")
            if isinstance(remotes, list):
                for position, value in enumerate(remotes):
                    if isinstance(value, str) and normalize(value) != value:
                        remotes[position] = normalize(value)
                        changed = True
            return changed

        return self._apply(mutate)

    # -- internals ----------------------------------------------------------

    def _canonical(self, where: str, value) -> str:
        if not isinstance(value, str):
            raise WriterError(
                self.path,
                f"`{where}` must be a remote URL string, got {type(value).__name__}",
            )
        return normalize(value)

    def _require_project(self, name):
        entries = self._section("projects", create=False)
        if entries is None or name not in entries:
            raise WriterError(self.path, f"`projects.{name}` is not in the registry.")
        return entries

    def _check_sources(self, mapping: Mapping) -> dict:
        """The writer's own checks on a ``set_sources`` mapping, before any
        mutation: shape, a non-empty string ``repo``, no credential."""
        if not isinstance(mapping, Mapping):
            raise WriterError(
                self.path,
                f"`set_sources` takes a mapping of source name to entry, got "
                f"{type(mapping).__name__}",
            )
        entries = {}
        for key, entry in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise WriterError(
                    self.path, f"a `sources:` name must be a non-empty string, got {key!r}"
                )
            if not isinstance(entry, Mapping):
                raise WriterError(
                    self.path, f"`sources.{key}` must be a mapping with a `repo:` key"
                )
            repo = entry.get("repo")
            if not isinstance(repo, str) or not repo.strip():
                raise WriterError(
                    self.path, f"`sources.{key}` must declare a non-empty string `repo:`"
                )
            if url_has_embedded_credential(repo):
                # Never echo the credentialed URL — the message must be safe
                # to print anywhere.
                raise WriterError(
                    self.path,
                    f"`sources.{key}.repo` embeds a credential "
                    f"({scrub_credentials(repo)} carried userinfo with a secret "
                    "component); the manifest never holds secrets — strip it and "
                    "let lmer derive auth at clone time.",
                )
            entries[key] = dict(entry)
        return entries

    def _refuse_merged(self, section, entries: dict) -> None:
        """Raise for a ``sources:`` entry built with a merge key when
        *entries* would change the section (module docstring, "Anchors and
        aliases"). Called by ``mutate`` so :meth:`_apply` rolls back."""
        merged = [key for key in _keys(section) if _has_merge(section[key])]
        if not merged:
            return
        held = {
            key: (
                {field: entry[field] for field in _keys(entry)}
                if isinstance(entry, _ruamel().Map)
                else entry
            )
            for key, entry in ((key, section[key]) for key in _keys(section))
        }
        if held == entries:
            return
        names = ", ".join(f"`sources.{key}`" for key in merged)
        raise WriterError(
            self.path,
            f"{names} {'is' if len(merged) == 1 else 'are'} assembled with a YAML "
            "merge key (`<<: *anchor`); a merged key reads through to another "
            "entry's node, so an update written key by key cannot keep the two "
            "apart — spell the entry out in full, then repeat the mutation.",
        )

    def _update_entry(self, section_slot, section, key, new: Mapping) -> bool:
        """Bring the existing ``sources.<key>`` to *new* scalar by scalar,
        keeping its position, style and comments."""
        entry = section[key]
        if not isinstance(entry, _ruamel().Map):
            _replace_value(section, key, _to_node(new))
            return True
        entry_slot = (section, key)
        changed = False
        for field in _keys(entry):
            if field not in new:
                _remove(entry_slot, entry, field)
                changed = True
        for field, value in new.items():
            if field in entry:
                # Reassigned only on a value change: a ruamel scalar compares
                # equal to its plain value, and leaving it alone keeps its
                # quoting style.
                if entry[field] != value:
                    _replace_value(entry, field, _to_node(value))
                    changed = True
            else:
                _insert(entry_slot, entry, len(entry), field, _to_node(value))
                changed = True
        return changed

    def _section(self, name: str, create: bool = True):
        """The top-level section *name* as a block mapping. A ``null`` or
        absent section becomes one when *create* is set — inserted at its
        documented position (``TOP_LEVEL_KEYS`` order), keeping a ``null``
        key's own line and comment — else ``None``."""
        doc = self._doc
        if name in doc and doc[name] is not None:
            section = doc[name]
            if len(section) == 0:
                section.fa.set_block_style()
            return section
        if not create:
            return None
        section = _to_node({})
        if name in doc:
            doc[name] = section
        else:
            before = TOP_LEVEL_KEYS[: TOP_LEVEL_KEYS.index(name)]
            keys = _keys(doc)
            last = max((i for i, key in enumerate(keys) if key in before), default=len(keys) - 1)
            _insert(None, doc, last + 1, name, section)
        return section

    def _apply(self, mutate: Callable[[], Optional[bool]]) -> bool:
        """Run *mutate* on the document, then the pre-write invariants and
        the atomic write. ``False`` from *mutate* means nothing changed and
        nothing is written. Any failure rolls the document back to the text
        on disk and re-raises."""
        try:
            if mutate() is False:
                return False
            text = _dump(self._doc)
            manifest = parse_manifest(text, path=self.path)
            build_registry(manifest)
            self._write(text)
        except BaseException:
            self._doc = _load(self._text)
            raise
        self._text = text
        self.manifest = manifest
        return True

    def _write(self, text: str) -> None:
        path = self.path
        try:
            current = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777
        except OSError as exc:
            raise WriterError(path, f"unreadable {MANIFEST_NAME}: {exc}") from exc
        if current != self._text:
            raise WriterError(
                path,
                f"{MANIFEST_NAME} changed on disk since it was loaded; reload it "
                "with `load_for_write` and repeat the mutation.",
            )
        _write_atomic(path, text, mode)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_for_write(path) -> ManifestWriter:
    """Open the manifest at *path* (the file, not the plexus root) for
    mutation.

    Refuses — with the loader's own :class:`~lmer_schema.manifest.ManifestError`
    or :class:`~lmer_schema.registry.RegistryError` — a ``schema:`` newer
    than this lmer, an unparseable or non-mapping file, and a registry that
    does not build; with :class:`WriterError` a missing file (use
    :func:`create`), an unreadable one, or a symlink. The PyYAML read path
    is the gate: ruamel opens the document only after it passed.
    """
    path = Path(path)
    if path.is_symlink():
        raise WriterError(
            path,
            f"{MANIFEST_NAME} is a symlink; the writer replaces the file "
            "atomically and will not swap a link for a file — open the target.",
        )
    if not path.exists():
        raise WriterError(
            path,
            f"no {MANIFEST_NAME} to write to. `create(path, name)` is the explicit "
            "bootstrap — it flips the tree to schema form (D20) and is never "
            "implied by a mutation.",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WriterError(path, f"unreadable {MANIFEST_NAME}: {exc}") from exc
    manifest = parse_manifest(text, path=path)
    build_registry(manifest)
    return ManifestWriter(path, text, manifest)


def create(path, name: str) -> ManifestWriter:
    """Bootstrap: write the minimal manifest (``schema: 1``, ``name:``) at
    *path* and return its handle.

    This is the write that flips a tree from legacy to schema form (D20),
    so it is its own explicit call: it refuses an existing file and
    registers nothing — ``add_project`` on the returned handle does that.
    """
    path = Path(path)
    if path.is_symlink() or path.exists():
        raise WriterError(
            path,
            f"{MANIFEST_NAME} already exists; open it with `load_for_write` — "
            "`create` bootstraps a tree that has no manifest and never overwrites one.",
        )
    if not isinstance(name, str) or not name.strip():
        raise WriterError(
            path, f"the instance `name:` must be a non-empty string, got {name!r}"
        )
    doc = _to_node({"schema": BOOTSTRAP_SCHEMA, "name": name})
    text = _dump(doc)
    manifest = parse_manifest(text, path=path)
    build_registry(manifest)
    _write_atomic(path, text, 0o666 & ~_umask())
    return ManifestWriter(path, text, manifest)
