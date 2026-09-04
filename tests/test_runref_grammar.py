"""One `host/project/slug` grammar, checked in both languages (#383 item 3).

The older guard (``tests/test_slack_run_references.py``) compares the two
languages on href verdicts only — whether a string is a reference at all. One
corpus drives both sides here, and it drives what those verdicts do not cover:
the composed key, and the host/project/slug a reference splits into.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from lmer_cli import runref
from lmer_platform import checkin, ctl, daemon, detect, runs
from matrix_bridge.threads import RunKey
from slack_chat import client as slack_client
from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"

#: ``(host, project, slug)`` triples. A project contributes several segments,
#: which is why a key cannot be split on "the second slash".
KEY_PARTS = [
    ("gitlab.example.com", "acme/widget", "develop-issue-383"),
    ("gitlab.example.com", "acme", "review-mr-284"),
    ("gitlab.example.com", "acme/group/sub", "masterplan"),
    ("h", "p", "s"),
    ("git.example.org", "a.b-c_d", "r-9"),
]

#: Whole references, and what each one is. ``None`` is "not a reference".
REFERENCES = {
    "lmer://run/gitlab.example.com/acme/widget/develop-issue-383":
        ("gitlab.example.com", "acme/widget", "develop-issue-383"),
    "lmer://run/h/p/s": ("h", "p", "s"),
    "lmer://run/gitlab.example.com/gh/acme/widget/r-9":
        ("gitlab.example.com", "gh/acme/widget", "r-9"),
    # Two segments is a project with no slug, or a slug with no project.
    "lmer://run/h/s": None,
    # A query would make a reference carry a verb, which the grammar refuses.
    "lmer://run/h/p/s?spawn=1": None,
    "lmer://run/h/p/s#frag": None,
    # Traversal, and the empty segments it needs.
    "lmer://run/h/p/s/../../etc/passwd": None,
    "lmer://run/h//s": None,
    "lmer://run/h/p/": None,
    # A leading punctuation character is not a segment start.
    "lmer://run/h/p/-s": None,
    "lmer://run/.hidden/p/s": None,
    # Not this scheme at all.
    "https://run/h/p/s": None,
    "lmer://runs/h/p/s": None,
    "lmer://run/h/p/s ": ("h", "p", "s"),  # surrounding space is trimmed
}

#: Padding where `str.strip()` and `String.trim()` disagree unless the Python
#: side names JavaScript's set explicitly. The first five Python strips and JS
#: does not; `\ufeff` goes the other way.
TRIM_PADS = [
    "\x85", "\x1c", "\x1d", "\x1e", "\x1f", "\ufeff",
    "\t", "\n", "\r", "\v", "\f", " ", "\xa0", "\u2000", "\u3000",
    "\u2028", "\u2029", "\u202f", "\u1680",
]
for _pad in TRIM_PADS:
    REFERENCES[f"{_pad}lmer://run/h/p/s{_pad}"] = (
        ("h", "p", "s") if _pad in runref.JS_TRIM else None
    )



# --- one definition per language --------------------------------------------

#: Every composer in the tree, as a callable taking the three parts.
#:
#: **Hand-maintained, and the sentinel below cannot tell you when it is short**:
#: it is parametrized over this dict, so anything missing is never called.
#: :func:`test_no_python_module_composes_the_key_itself` is the scan that holds
#: the invariant — nobody spells the key themselves — and this dict is the proof
#: that the ones producing a run's identity delegate. Deliberately not the same
#: set: `workrepo` composes the key inside log-dedupe strings, which is a call
#: site and not a composer.
COMPOSERS = {
    "runs.run_key": lambda h, p, s: runs.run_key(h, p, s),
    "checkin._run_key": lambda h, p, s: checkin._run_key(h, p, s),
    "checkin.StaleRun.ref": lambda h, p, s: checkin.StaleRun(
        host=h, project=p, slug=s, label="l", state="dormant",
        since=None, age_seconds=None,
    ).ref,
    "detect.Signal.ref": lambda h, p, s: detect.Signal(
        host=h, project=p, slug=s, kind="crashed",
    ).ref,
    "detect.SessionSignal.ref": lambda h, p, s: detect.SessionSignal(
        session="s-1", entry_id="e-1", seq=1, text="t", at="2026-09-03T00:00:00Z",
        host=h, project=p, slug=s,
    ).ref,
    "matrix RunKey.__str__": lambda h, p, s: str(RunKey(h, p, s)),
}

#: Every splitter, as a callable returning ``(host, project, slug)``.
SPLITTERS = {
    "ctl._run_fields": lambda key: tuple(
        ctl._run_fields(key)[f] for f in ("host", "project", "slug")
    ),
    "daemon._split_run_ref": lambda key: daemon._split_run_ref(key),
    "matrix RunKey.parse": lambda key: (
        lambda k: (k.host, k.project, k.slug)
    )(RunKey.parse(key)),
}


@pytest.mark.parametrize("name", sorted(COMPOSERS))
@pytest.mark.parametrize("host, project, slug", KEY_PARTS)
def test_every_python_composer_spells_the_key_the_same(name, host, project, slug):
    """A key that disagrees is not a crash — it is a row nothing matches."""
    assert COMPOSERS[name](host, project, slug) == runref.compose(
        host, project, slug
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_every_composer_actually_goes_through_the_shared_one(name, monkeypatch):
    """The assertion above passes for a copy that merely agrees today; a
    sentinel is what separates delegating from duplicating."""
    monkeypatch.setattr(runref, "compose", lambda h, p, s: "SENTINEL")

    assert COMPOSERS[name]("h", "p", "s") == "SENTINEL", (
        f"{name} composes the key itself instead of calling runref.compose"
    )


@pytest.mark.parametrize("name", sorted(SPLITTERS))
def test_every_splitter_actually_goes_through_the_shared_one(name, monkeypatch):
    """The same sentinel, the other way round. `daemon._split_run_ref` normalises
    its input first — deliberately — but the *split* has to be the shared one."""
    monkeypatch.setattr(runref, "split", lambda key: ("SENT", "IN", "EL"))

    assert SPLITTERS[name]("h/p/s") == ("SENT", "IN", "EL"), (
        f"{name} splits the key itself instead of calling runref.split"
    )


def test_the_shell_facing_splitter_keeps_its_own_tolerance():
    """Delegating the grammar must not take the daemon's normalisation with it:
    `lmer platform adopt /h/p/s/` is what a human pastes."""
    assert daemon._split_run_ref(" /h/p/s/ ") == ("h", "p", "s")
    assert daemon._split_run_ref("h//p/s") == ("h", "p", "s")
    # The shared splitter repairs nothing: two spellings must not become one run
    # everywhere, only where somebody typed it.
    assert runref.split(" /h/p/s/ ") == (" ", "h/p/s", " ")


@pytest.mark.parametrize("host, project, slug", KEY_PARTS)
def test_the_splitter_is_the_composer_run_backwards(host, project, slug):
    key = runref.compose(host, project, slug)

    assert runref.split(key) == (host, project, slug)
    assert ctl._run_fields(key) == {
        "host": host, "project": project, "slug": slug,
    }


def test_the_splitter_stays_permissive_about_what_it_is_handed():
    """``ctl`` shapes an argument and stops there: a key naming no run must reach
    the daemon and come back as a refusal that names the field."""
    assert runref.split("HOST/Project Name/a slug") == (
        "HOST", "Project Name", "a slug"
    )
    assert runref.split("h//s") == ("h", "", "s")


@pytest.mark.parametrize("key", ["", "h", "h/p", "/", None, 7])
def test_a_key_with_fewer_than_three_parts_is_not_one(key):
    assert runref.split(key) is None
    if isinstance(key, str):
        with pytest.raises(ctl.CtlError):
            ctl._run_fields(key)


def test_the_index_still_refuses_an_empty_part():
    """Delegating the spelling must not delegate away the validation."""
    with pytest.raises(runs.RunIndexError):
        runs.run_key("h", "  ", "s")


def test_the_slack_stripper_uses_the_shared_grammar():
    """The bridge keeps the name its callers reach for, not a second pattern."""
    assert slack_client.strip_run_references(
        "[auth fix](lmer://run/h/p/s) is done"
    ) == "auth fix is done"
    assert not hasattr(slack_client, "_RUN_REF_LINK"), (
        "slack_chat holds its own copy of the reference pattern again"
    )


# --- the same corpus, in both languages -------------------------------------

def _python_verdicts():
    """What the Python grammar says about every href in the corpus."""
    verdicts = {}
    for href in REFERENCES:
        parsed = runref.parse_reference(href)
        verdicts[href] = None if parsed is None else [
            parsed["host"], parsed["project"], parsed["slug"], parsed["key"],
        ]
    return verdicts


def test_the_corpus_is_what_the_python_side_claims():
    """A corpus only the two implementations agree on would pin whatever they
    both got wrong, so the expectations are asserted before they are a yardstick."""
    for href, expected in REFERENCES.items():
        parsed = runref.parse_reference(href)
        if expected is None:
            assert parsed is None, href
            continue
        host, project, slug = expected
        assert parsed == {
            "host": host, "project": project, "slug": slug,
            "key": runref.compose(host, project, slug),
        }, href


def test_the_two_languages_parse_the_corpus_identically():
    """Not just "is it a reference" — what its parts *are*, and the key they
    compose to."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = "\n".join([
        "import { parseRunRef, runKey } from './src/runref.js'",
        f"const hrefs = {json.dumps(sorted(REFERENCES))}",
        "const out = {}",
        "for (const href of hrefs) {",
        "  const ref = parseRunRef(href)",
        "  out[href] = ref === null",
        "    ? null",
        "    : [ref.host, ref.project, ref.slug, runKey(ref)]",
        "}",
        "console.log(JSON.stringify(out))",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    assert json.loads(result.stdout) == _python_verdicts(), (
        "lmer_cli/runref.py and web/src/runref.js disagree about a run reference"
    )


SRC = Path(__file__).resolve().parent.parent / "src"

#: An f-string composing *this* key. Named fields rather than any three-part
#: f-string, which also matches unrelated URLs (`ui_build.download_url`).
INLINE_KEY_RE = re.compile(
    r"\{[^\"}]*\bhost\b[^\"}]*\}/\{[^\"}]*\bproject\b[^\"}]*\}"
    r"/\{[^\"}]*\bslug\b[^\"}]*\}"
)


def test_no_python_module_composes_the_key_itself():
    """The backstop `COMPOSERS` cannot be: a scan rather than a list.

    It does not know *who* should be calling `runref.compose` — it knows nobody
    may spell the key themselves, which is the property item 3 asked for.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "runref.py":
            continue  # the one definition, which has to spell it somewhere
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if INLINE_KEY_RE.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, (
        f"{offenders} compose a run key inline — call lmer_cli.runref.compose, "
        "and add the new call site to COMPOSERS so the sentinel covers it"
    )


def test_the_components_do_not_spell_the_key_themselves():
    """The drift this consolidates: RunNav.vue carried a comment saying it had to
    stay in step with App.vue by hand."""
    offenders = [
        path.relative_to(WEB)
        for path in sorted((WEB / "src").rglob("*.vue"))
        if "}/${" in path.read_text(encoding="utf-8").replace(" ", "")
        and ".host}/${" in path.read_text(encoding="utf-8").replace(" ", "")
    ]
    assert not offenders, (
        f"{offenders} compose a run key inline — import runKey from runref.js"
    )
