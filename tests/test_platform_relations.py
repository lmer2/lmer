"""Runs that belong together, and the tap between them (issue #141, T53).

The operator's case is the followup cycle: an orchestrating assistant relates a develop
run and the review run it started for it, and from then on crossing between the
two is two taps instead of a hunt through a list of runs whose names differ by one
word.

Most of what is pinned here is about the *shape* of that relation, because every
interesting failure is a shape failure:

- **the relation is symmetric, and it is symmetric by storage rather than by a
  write path.** Each one is stored once under a canonical pair key, so relating A
  to B shows on B, removing it from either end removes both directions, and there
  is no state in which one run links to a run that does not link back. A mirrored
  two-sided store would make all three of those a property of remembering to write
  twice — including for the operator hand-editing the file, which spec D2 says is a
  supported way to repair platform state
- **spec D3: the platform never writes run state.** Its copy of the work repo is a
  mirror the daemon force-resets on every pull, so a relation stored beside
  ``state.yaml`` would be destroyed by the next fetch with no error anywhere. This
  is the guard that would have caught the obvious implementation
- a relation write never touches ``runs.json``, for the reason
  :mod:`tests.test_platform_meta` gives about metadata: a lost update there costs a
  *run*, and a row disappearing from the fleet is much worse than a lost relation
- **a relation naming a run this orchestrator does not track is legible, not
  dead.** Relating ahead of adoption is allowed and forgetting a run leaves its
  relations alone, so the daemon reports ``tracked: false`` and the UI shows the
  key with a hint instead of a switch that would land on a blank page. Reads
  emphatically do not prune: a read that deletes would throw away a deliberate
  statement the first time anything looked at the run
- both ends must parse as run keys, with one grammar
  (:func:`lmer_platform.runs.run_key`) and never a second, and the refusal says
  which of the two runs it was about — every request here carries two runs with the
  same three field names
- the write path is the API, reachable by anything holding the shared secret,
  which is how the assistant relates the review run it just started (T30)
- the UI element is at the *bottom of the detail view and inside no tab*, because
  the point of a switcher is switching while looking at something else. That half
  is pinned in :mod:`tests.test_platform_web_details_tabs`, which owns what is in a
  panel and what is not

How it looks, and whether the chips crowd a phone, are verified by building the
bundle and by live test LT3 on a real phone.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import meta, relations, runs, store
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"

WEB = Path(__file__).resolve().parent.parent / "web"
APP = WEB / "src" / "App.vue"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
RELATED_RUNS = WEB / "src" / "components" / "RelatedRuns.vue"
API_CLIENT = WEB / "src" / "api.js"

HOST, PROJECT = "gitlab.example.com", "agents/global"
DEVELOP = (HOST, PROJECT, "develop-issue-141")
REVIEW = (HOST, PROJECT, "review-mr-168")

BODY = {"host": DEVELOP[0], "project": DEVELOP[1], "slug": DEVELOP[2]}
RELATED = {"host": REVIEW[0], "project": REVIEW[1], "slug": REVIEW[2]}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def tracked(platform_root):
    """The motivating case: a develop run and the review run of it, both tracked."""
    runs.track(*DEVELOP, source="spawned", taskdef="develop")
    runs.track(*REVIEW, source="spawned", taskdef="review")
    return DEVELOP, REVIEW


@pytest.fixture
def client(platform_root):
    """The app, with a canned fleet view so no route here needs a work repo."""
    config = cfg.load()
    return TestClient(
        api.create_app(
            config, SECRET, state_builder=lambda config, force_pull=False: {},
        )
    )


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def _read(path):
    return path.read_text(encoding="utf-8")


def _keys(entries):
    return [entry.key for entry in entries]


#: A ``last_seen`` far enough in the past that any rewrite of the index changes it.
#: Without it the index guard could pass vacuously: ``track()`` stamps a
#: whole-second timestamp, so a rewrite in the same second writes the same bytes.
AGED = "2026-01-01T00:00:00Z"


def _age_the_index():
    """Backdate the tracked entries, and hand back the index file's exact bytes."""
    index = runs.load_index()
    for entry in index.values():
        entry["last_seen"] = AGED
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})
    return store.snapshot_path(runs.RUNS_FILE)


# --- where it lives, and why (spec D3) ---------------------------------------

def _mirror_contents(config):
    return {
        path: path.read_bytes()
        for path in sorted(config.mirror_path.rglob("*"))
        if path.is_file()
    }


def test_relations_are_platform_state_and_never_reach_the_work_repo(
    tracked, platform_root
):
    """The constraint the whole design comes from.

    The daemon's mirror is read-only and force-reset on every pull, so a relation
    written into a run's directory survives exactly until the next fetch and then
    vanishes with no error anywhere. A planted state.yaml is included because
    "wrote nothing new" and "left what was there alone" are different claims and
    both have to hold.
    """
    config = cfg.load()
    run_dir = config.mirror_path / HOST / PROJECT / "runs" / DEVELOP[2]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\n", encoding="utf-8"
    )
    before = _mirror_contents(config)

    relations.relate(DEVELOP, REVIEW)

    assert _mirror_contents(config) == before, (
        "a relation reached the work-repo mirror, which the next pull destroys"
    )
    assert (platform_root / relations.RELATIONS_FILE).is_file(), (
        "the relation is not in platform state either — where did it go?"
    )


def test_relating_two_runs_does_not_rewrite_the_run_index(tracked, platform_root):
    """The reason this is its own snapshot rather than a field on runs.json.

    ``store`` gives each write atomicity but explicitly not consistency across a
    read-modify-write, and the API's handlers are synchronous so Starlette runs
    them concurrently in a threadpool. Sharing one file means a relation write
    racing a spawn's ``track()`` can drop a tracked *run* — a row vanishing from
    the fleet — where separate files make the worst case a lost relation.
    """
    index = _age_the_index()
    before = index.read_bytes()

    relations.relate(DEVELOP, REVIEW)

    assert index.read_bytes() == before, (
        "relating two runs rewrote the tracked-run index, so it can now lose a run"
    )
    assert store.snapshot_path(relations.RELATIONS_FILE) != index


def test_the_snapshot_is_human_readable(tracked, platform_root):
    """D2's point: repairable in an editor when the orchestrator is wedged.

    Which is also why the pair key is the two run keys joined rather than a hash of
    them: what a line of this file means has to be readable at a glance.
    """
    relations.relate(DEVELOP, REVIEW)
    payload = json.loads(_read(store.snapshot_path(relations.RELATIONS_FILE)))

    pair = f"{runs.run_key(*DEVELOP)}+{runs.run_key(*REVIEW)}"
    assert list(payload["relations"]) == [pair], (
        f"stored under {list(payload['relations'])}"
    )
    assert payload["relations"][pair]["runs"] == [
        {"host": DEVELOP[0], "project": DEVELOP[1], "slug": DEVELOP[2]},
        {"host": REVIEW[0], "project": REVIEW[1], "slug": REVIEW[2]},
    ]
    assert payload["relations"][pair]["created_at"], "nothing records when"
    assert payload["schema"] == store.SCHEMA_VERSION


def test_the_entry_names_its_runs_in_full_rather_than_only_in_its_key(
    tracked, platform_root
):
    """Why there is a ``runs`` list at all, given the key already has both keys.

    A run key is ``host/project/slug`` and a project is ``group/subgroup``, so
    recovering the three parts from the string means guessing where the project
    ends — and every guess is wrong for some legitimate run. The parts are stored,
    so nothing ever has to guess.
    """
    relations.relate(DEVELOP, REVIEW)

    entry = relations.load_all()[f"{runs.run_key(*DEVELOP)}+{runs.run_key(*REVIEW)}"]
    assert [ref["project"] for ref in entry["runs"]] == [PROJECT, PROJECT]
    assert "/" in PROJECT, "the fixture no longer exercises a nested project"
    assert relations.list_for(*DEVELOP)[0].project == PROJECT, (
        "the project came back split at the wrong slash"
    )


# --- symmetry: the point of storing it once ----------------------------------

def test_relating_a_to_b_shows_on_b(tracked, platform_root):
    """The headline guarantee, and the one a two-sided store gets wrong.

    "Switch between them" has no direction: from the review you want the run under
    review, and from that run you want its review. A one-sided store means a run
    does not know what points at it.
    """
    relations.relate(DEVELOP, REVIEW)

    assert _keys(relations.list_for(*DEVELOP)) == [runs.run_key(*REVIEW)]
    assert _keys(relations.list_for(*REVIEW)) == [runs.run_key(*DEVELOP)], (
        "the relation is one-sided: the run that was named does not know about it"
    )


def test_either_order_is_the_same_single_relation(tracked, platform_root):
    """Stored once under a canonical key, which is what makes symmetry structural.

    Two entries for one relation is how the two halves start disagreeing — and it
    is what makes removal a two-step operation with a half-done state.
    """
    assert relations.relate(DEVELOP, REVIEW) is True
    assert relations.relate(REVIEW, DEVELOP) is False, (
        "relating the same pair the other way round created a second relation"
    )

    assert len(relations.load_all()) == 1, f"stored {list(relations.load_all())}"
    assert len(relations.list_for(*DEVELOP)) == 1
    assert len(relations.list_for(*REVIEW)) == 1


def test_relating_the_same_pair_twice_changes_nothing(tracked, platform_root):
    """Idempotent so an assistant can relate without reading first."""
    relations.relate(DEVELOP, REVIEW)
    before = store.snapshot_path(relations.RELATIONS_FILE).read_bytes()

    assert relations.relate(DEVELOP, REVIEW) is False
    assert store.snapshot_path(relations.RELATIONS_FILE).read_bytes() == before


def test_a_run_can_be_related_to_several_and_each_only_sees_its_own(
    platform_root
):
    runs.track(HOST, PROJECT, "hub")
    for slug in ("one", "two"):
        runs.track(HOST, PROJECT, slug)
        relations.relate((HOST, PROJECT, "hub"), (HOST, PROJECT, slug))

    assert _keys(relations.list_for(HOST, PROJECT, "hub")) == [
        f"{HOST}/{PROJECT}/one", f"{HOST}/{PROJECT}/two",
    ], "the switcher's order is not stable, so a page renders differently twice"
    assert _keys(relations.list_for(HOST, PROJECT, "one")) == [
        f"{HOST}/{PROJECT}/hub"
    ]


# --- removal ------------------------------------------------------------------

def test_removing_a_relation_removes_both_directions(tracked, platform_root):
    """One entry *is* the relation, so there is no half-removed state in which one
    run still links to the other."""
    relations.relate(DEVELOP, REVIEW)

    assert relations.unrelate(DEVELOP, REVIEW) is True

    assert relations.list_for(*DEVELOP) == []
    assert relations.list_for(*REVIEW) == [], (
        "the relation was removed from one side only, so the other still links to "
        "a run that no longer links back"
    )
    assert relations.load_all() == {}


def test_removing_it_from_the_other_end_works_the_same(tracked, platform_root):
    """Whichever page the operator is on when they decide it was a mistake."""
    relations.relate(DEVELOP, REVIEW)

    assert relations.unrelate(REVIEW, DEVELOP) is True
    assert relations.load_all() == {}


def test_removing_a_relation_that_is_not_there_is_not_an_error(
    tracked, platform_root
):
    """The caller asked for it to be gone, and it is."""
    assert relations.unrelate(DEVELOP, REVIEW) is False


def test_removing_one_relation_leaves_the_others_alone(platform_root):
    runs.track(HOST, PROJECT, "hub")
    for slug in ("keep", "drop"):
        relations.relate((HOST, PROJECT, "hub"), (HOST, PROJECT, slug))

    relations.unrelate((HOST, PROJECT, "drop"), (HOST, PROJECT, "hub"))

    assert _keys(relations.list_for(HOST, PROJECT, "hub")) == [
        f"{HOST}/{PROJECT}/keep"
    ]


# --- a run this orchestrator does not track ------------------------------------

def test_relating_a_run_this_fleet_does_not_track_is_allowed(platform_root):
    """Relating ahead of adoption is legitimate: an assistant may relate a run it
    has just spawned or intends to adopt next, and refusing would make correctness
    depend on an ordering the caller cannot see."""
    runs.track(*DEVELOP)

    assert relations.relate(DEVELOP, (HOST, PROJECT, "never-adopted")) is True
    assert _keys(relations.list_for(*DEVELOP)) == [f"{HOST}/{PROJECT}/never-adopted"]


def test_a_relation_naming_an_untracked_run_is_flagged_rather_than_hidden(
    platform_root
):
    """The legibility requirement: a relation that cannot be followed must read as
    a key with a reason, not as a dead link and not as nothing at all."""
    runs.track(*DEVELOP)
    relations.relate(DEVELOP, REVIEW)

    entry = relations.list_for(*DEVELOP)[0]
    assert entry.key == runs.run_key(*REVIEW), "the relation vanished on read"
    assert entry.tracked is False, (
        "an untracked run is reported as tracked, so the UI offers a switch to a "
        "run the fleet cannot select — which lands on a blank page"
    )
    assert relations.list_for(*REVIEW)[0].tracked is True


def test_forgetting_a_run_keeps_its_relations(tracked, platform_root):
    """The opposite of what ``forget`` does to metadata, and deliberately so.

    A title describes a run in this fleet and dies with it; a relation is a
    statement about *two* runs and is still true — and still useful to the end that
    is still tracked — after one of them is forgotten. Re-adopting the run gets the
    relation back, which pruning would have made impossible.
    """
    relations.relate(DEVELOP, REVIEW)

    assert runs.forget(*REVIEW) is True

    surviving = relations.list_for(*DEVELOP)
    assert _keys(surviving) == [runs.run_key(*REVIEW)], (
        "forgetting one run removed a relation the other run still shows"
    )
    assert surviving[0].tracked is False, "a forgotten run still reads as tracked"

    runs.track(*REVIEW, source="adopted")
    assert relations.list_for(*DEVELOP)[0].tracked is True


def test_reading_relations_never_writes(platform_root):
    """Pruning on read was the alternative, and this is why it was rejected.

    ``store``'s whole convention is that writes are loud and reads are quiet, and a
    read that deletes is neither: it would throw away an assistant's deliberate
    statement the first time anything looked at the run, and a run forgotten in one
    tab would lose its relations to a refresh in another.
    """
    runs.track(*DEVELOP)
    relations.relate(DEVELOP, (HOST, PROJECT, "never-adopted"))
    path = store.snapshot_path(relations.RELATIONS_FILE)
    before = path.read_bytes()

    for _ in range(3):
        assert len(relations.list_for(*DEVELOP)) == 1

    assert path.read_bytes() == before, "reading pruned the store"
    assert not list(path.parent.glob(f"{relations.RELATIONS_FILE}.bad-*"))


# --- what is refused ----------------------------------------------------------

@pytest.mark.parametrize(
    "bad", [None, "", "   ", 17, True, ["h", "p", "s"], {"host": "h"}]
)
def test_a_part_that_is_not_a_run_key_is_refused(tracked, platform_root, bad):
    """One grammar for a run's identity, and it is ``runs.run_key``'s. Not
    coerced: ``str(value)`` would relate a run called "None"."""
    with pytest.raises(relations.RelationError):
        relations.relate(DEVELOP, (HOST, PROJECT, bad))

    assert relations.load_all() == {}, "a refused relation stored something anyway"


def test_a_refusal_says_which_of_the_two_runs_was_wrong(tracked, platform_root):
    """Every request here carries two runs with the same three field names, so
    "slug must be a non-empty string" alone does not say which one to fix."""
    with pytest.raises(relations.RelationError) as refusal:
        relations.relate(DEVELOP, (HOST, PROJECT, None))
    assert "related run" in str(refusal.value) and "slug" in str(refusal.value)

    with pytest.raises(relations.RelationError) as other:
        relations.relate((None, PROJECT, "x"), REVIEW)
    assert str(other.value).startswith("run:"), (
        f"the subject run's refusal reads {str(other.value)!r}"
    )
    assert "related" not in str(other.value)


@pytest.mark.parametrize(
    "run", [None, 17, "gitlab.example.com/agents/global/x", "abc", ("h", "p")]
)
def test_a_run_that_is_not_three_parts_is_refused_with_its_shape(
    tracked, platform_root, run
):
    """Including the composite key as a single string, which is the natural
    mistake: it is what the reply shows, and it is not what this takes.

    ``"abc"`` is in here for the specific reason a string is rejected before it is
    unpacked: a three-character one unpacks into three parts, and would quietly
    relate a run called ``a/b/c``.
    """
    with pytest.raises(relations.RelationError, match="host, project and slug"):
        relations.relate(DEVELOP, run)


def test_a_run_cannot_be_related_to_itself(tracked, platform_root):
    """A relation is a run to switch to, and this one is the run you are on. Stored,
    it would render a chip pointing at the page it is on."""
    with pytest.raises(relations.RelationError, match="itself"):
        relations.relate(DEVELOP, DEVELOP)

    with pytest.raises(relations.RelationError, match="itself"):
        relations.unrelate(DEVELOP, DEVELOP)
    assert relations.load_all() == {}


def test_a_run_key_containing_the_pair_separator_is_refused(tracked, platform_root):
    """The one thing the pair key cannot survive, refused loudly rather than
    encoded quietly.

    ``a+b`` related to ``c`` and ``a`` related to ``b+c`` produce the same pair key,
    so one would overwrite the other's relation with nothing said. A loud refusal on
    a run nobody has (slugs are sanitised) beats a silent loss on the one who does.
    """
    with pytest.raises(relations.RelationError) as refusal:
        relations.relate(DEVELOP, (HOST, PROJECT, "with+a+plus"))

    assert relations.PAIR_SEPARATOR in str(refusal.value)
    assert relations.load_all() == {}


def test_the_per_run_cap_is_refused_with_both_numbers(platform_root):
    """Bounded rather than trusted: an agent writes these, and the element is
    always visible at the bottom of a phone screen. The refusal carries the count
    and the limit because the caller's next move depends on the difference."""
    for index in range(relations.MAX_RELATIONS_PER_RUN):
        relations.relate(DEVELOP, (HOST, PROJECT, f"other-{index:02d}"))

    with pytest.raises(relations.RelationError) as refusal:
        relations.relate(DEVELOP, (HOST, PROJECT, "one-too-many"))

    assert str(relations.MAX_RELATIONS_PER_RUN) in str(refusal.value)
    assert "unrelate" in str(refusal.value), "the refusal names no way through"
    assert len(relations.list_for(*DEVELOP)) == relations.MAX_RELATIONS_PER_RUN


def test_restating_an_existing_relation_at_the_cap_is_not_refused(platform_root):
    """The duplicate is checked before the cap: re-stating a relation that already
    exists moves no limit, and an assistant that relates without reading first must
    not start failing once a run is full."""
    for index in range(relations.MAX_RELATIONS_PER_RUN):
        relations.relate(DEVELOP, (HOST, PROJECT, f"other-{index:02d}"))

    assert relations.relate(DEVELOP, (HOST, PROJECT, "other-00")) is False


# --- durability ---------------------------------------------------------------

def test_a_corrupt_snapshot_reads_as_empty_and_is_logged(
    tracked, platform_root, caplog
):
    """One bad file must not take a run's detail view down with it, and the bad
    bytes are the only record of what went wrong — so they are moved aside."""
    path = store.snapshot_path(relations.RELATIONS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert relations.list_for(*DEVELOP) == []
    assert any(
        "platform_run_relations_unreadable" in r.message for r in caplog.records
    )
    assert len(list(path.parent.glob("relations.json.bad-*"))) == 1


@pytest.mark.parametrize(
    "entry",
    [
        "not a mapping",
        {"runs": "not a list"},
        {"runs": []},
        {"runs": [{"host": "h", "project": "p", "slug": "a"}]},
        {"runs": [{"host": "h", "project": "p", "slug": "a"}, "not a mapping"]},
        {"runs": [
            {"host": "h", "project": "p", "slug": "a"},
            {"host": "h", "project": "p"},
        ]},
    ],
)
def test_a_malformed_entry_is_skipped_rather_than_fatal(
    tracked, platform_root, caplog, entry
):
    """Hand-edited, or written by a version that changed shape. One unusable entry
    must not empty every run's relations."""
    store.write_json(
        store.snapshot_path(relations.RELATIONS_FILE),
        {"relations": {"h/p/a+h/p/b": entry}},
    )

    assert relations.list_for("h", "p", "a") == []
    assert any("platform_run_relation_malformed" in r.message for r in caplog.records)


def test_an_entry_whose_runs_do_not_rebuild_its_key_is_skipped(
    tracked, platform_root, caplog
):
    """The check that is specific to this file, and the reason for it: the key is
    the pair's identity and the ``runs`` list is the data. An entry where they
    disagree is ambiguous about which run it points at, and a guess there is a link
    to the wrong run."""
    store.write_json(
        store.snapshot_path(relations.RELATIONS_FILE),
        {"relations": {"h/p/a+h/p/b": {"runs": [
            {"host": "h", "project": "p", "slug": "a"},
            {"host": "h", "project": "p", "slug": "somewhere-else"},
        ]}}},
    )

    assert relations.list_for("h", "p", "a") == []
    assert relations.list_for("h", "p", "somewhere-else") == []
    assert any("platform_run_relation_malformed" in r.message for r in caplog.records)


def test_a_run_related_to_itself_on_disk_is_skipped(tracked, platform_root, caplog):
    """Refused on the way in, so it can only be hand-written — and it would render
    a chip pointing at the page it is on."""
    store.write_json(
        store.snapshot_path(relations.RELATIONS_FILE),
        {"relations": {"h/p/a+h/p/a": {"runs": [
            {"host": "h", "project": "p", "slug": "a"},
            {"host": "h", "project": "p", "slug": "a"},
        ]}}},
    )

    assert relations.list_for("h", "p", "a") == []
    assert any("platform_run_relation_malformed" in r.message for r in caplog.records)


def test_one_malformed_entry_does_not_hide_the_good_ones(tracked, platform_root):
    relations.relate(DEVELOP, REVIEW)
    entries = relations.load_all()
    entries["h/p/a+h/p/b"] = "not a mapping"
    store.write_json(
        store.snapshot_path(relations.RELATIONS_FILE), {"relations": entries}
    )

    assert _keys(relations.list_for(*DEVELOP)) == [runs.run_key(*REVIEW)]


def test_a_snapshot_without_a_relations_key_reads_as_empty(tracked, platform_root):
    store.write_json(
        store.snapshot_path(relations.RELATIONS_FILE), {"something": "else"}
    )

    assert relations.list_for(*DEVELOP) == []


# --- over the API --------------------------------------------------------------

def test_the_relation_routes_require_the_secret(client):
    assert client.get("/api/runs/relations").status_code == 401
    assert client.post("/api/runs/relate", json=BODY).status_code == 401
    assert client.post("/api/runs/unrelate", json=BODY).status_code == 401


def test_relating_and_reading_over_the_api(client, tracked):
    written = client.post(
        "/api/runs/relate", headers=bearer_header(),
        json={**BODY, "related": RELATED},
    )
    assert written.status_code == 200
    assert written.json()["created"] is True
    assert written.json()["related"] == RELATED
    assert [entry["key"] for entry in written.json()["relations"]] == [
        runs.run_key(*REVIEW)
    ]

    read = client.get("/api/runs/relations", headers=bearer_header(), params=BODY)
    assert read.status_code == 200
    assert read.json()["relations"] == written.json()["relations"], (
        "the write's reply is not what a read of the same run answers"
    )
    assert read.json()["run"] == BODY

    # And the other end knows, over the same route, without having been written to.
    other = client.get("/api/runs/relations", headers=bearer_header(), params=RELATED)
    assert [entry["key"] for entry in other.json()["relations"]] == [
        runs.run_key(*DEVELOP)
    ]


def test_the_write_route_is_reachable_with_the_shared_secret_alone(client, tracked):
    """How the assistant relates the review run it just started (T30): the same
    bearer credential every other client uses, over the same REST surface — a
    header, a body, and no session, ticket or container anywhere in it."""
    response = client.post(
        "/api/runs/relate",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={**BODY, "related": RELATED},
    )

    assert response.status_code == 200
    assert _keys(relations.list_for(*DEVELOP)) == [runs.run_key(*REVIEW)]


def test_relating_twice_over_the_api_is_a_200_that_says_it_changed_nothing(
    client, tracked
):
    payload = {**BODY, "related": RELATED}
    client.post("/api/runs/relate", headers=bearer_header(), json=payload)
    again = client.post("/api/runs/relate", headers=bearer_header(), json=payload)

    assert again.status_code == 200
    assert again.json()["created"] is False


def test_unrelating_over_the_api_removes_both_directions(client, tracked):
    client.post(
        "/api/runs/relate", headers=bearer_header(), json={**BODY, "related": RELATED},
    )

    # Named the other way round on purpose: either end can undo it.
    removed = client.post(
        "/api/runs/unrelate", headers=bearer_header(),
        json={**RELATED, "related": BODY},
    )

    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert removed.json()["relations"] == []
    assert relations.list_for(*DEVELOP) == []


def test_unrelating_something_that_was_not_related_is_not_a_404(client, tracked):
    response = client.post(
        "/api/runs/unrelate", headers=bearer_header(),
        json={**BODY, "related": RELATED},
    )

    assert response.status_code == 200
    assert response.json()["removed"] is False


def test_relating_an_untracked_run_over_the_api_is_allowed_and_flagged(
    client, platform_root
):
    runs.track(*DEVELOP)
    payload = client.post(
        "/api/runs/relate", headers=bearer_header(), json={**BODY, "related": RELATED},
    ).json()

    assert payload["relations"][0]["tracked"] is False
    assert payload["relations"][0]["key"] == runs.run_key(*REVIEW), (
        "the reply gives the UI nothing to name the run with"
    )


def test_a_related_field_of_the_wrong_shape_is_a_400_naming_the_shape(client, tracked):
    """The natural mistake is sending the composite key as a string, because that
    is what the reply shows."""
    response = client.post(
        "/api/runs/relate", headers=bearer_header(),
        json={**BODY, "related": runs.run_key(*REVIEW)},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "related must be an object" in detail
    assert '"host"' in detail and '"slug"' in detail


@pytest.mark.parametrize(
    "body", [{}, {"host": HOST}, {"host": HOST, "project": PROJECT}]
)
def test_an_incomplete_run_reference_is_a_400_that_names_the_field(client, body):
    """The same refusal ``/api/runs/adopt`` gives, rather than a validation error in
    a different shape for the same mistake."""
    response = client.post(
        "/api/runs/relate", headers=bearer_header(),
        json={**body, "related": RELATED},
    )

    assert response.status_code == 400
    assert "non-empty string" in response.json()["detail"]


def test_a_read_with_no_run_named_is_a_400_rather_than_a_validation_error(client):
    response = client.get("/api/runs/relations", headers=bearer_header())

    assert response.status_code == 400
    assert "host" in response.json()["detail"]


def test_every_reply_says_the_relations_are_local_to_this_orchestrator(
    client, tracked
):
    """The surprising half, and the one a client would otherwise assume the other
    way round: this does not travel with the runs."""
    payload = client.get(
        "/api/runs/relations", headers=bearer_header(), params=BODY
    ).json()

    assert payload["local"] is True
    note = payload["note"].lower()
    assert "platform state" in note and "work repo" in note
    assert "forgetting a run does not remove" in note, (
        "nothing says a relation outlives the run it names"
    )


def test_the_reply_carries_the_cap_and_this_orchestrators_titles(client, tracked):
    """The cap so a UI counts against the number that will actually refuse the next
    relation, and the title because a listing names a run by it when it has one
    (T65) — two slugs differing by one word is the hunt this feature ends."""
    meta.write(*REVIEW, title="review of the orchestrator spec")
    client.post(
        "/api/runs/relate", headers=bearer_header(), json={**BODY, "related": RELATED},
    )

    payload = client.get(
        "/api/runs/relations", headers=bearer_header(), params=BODY
    ).json()

    assert payload["limits"] == {"relations": relations.MAX_RELATIONS_PER_RUN}
    assert payload["relations"][0]["title"] == "review of the orchestrator spec"


def test_the_route_list_documents_all_three_verbs_and_the_locality(client):
    body = client.get("/api", headers=bearer_header()).text

    assert "GET  /api/runs/relations" in body
    assert "POST /api/runs/relate" in body
    assert "POST /api/runs/unrelate" in body
    assert "never\n" in body or "never the work repo" in body
    assert "SYMMETRIC" in body, (
        "the route list does not say the relation goes both ways, which is the one "
        "thing a client would otherwise implement twice"
    )


# --- the UI ---------------------------------------------------------------------

def test_the_switcher_lives_at_the_bottom_of_the_detail_view():
    """Below the tab window and inside no panel. The panel-level guard lives in
    tests/test_platform_web_details_tabs.py, which owns that parse; this pins the
    mount and the import."""
    detail = _read(RUN_DETAIL)

    assert "import RelatedRuns from './RelatedRuns.vue'" in detail
    assert detail.count("<RelatedRuns") == 1, "the switcher is mounted twice"
    assert detail.index("<RelatedRuns") > detail.index("</v-tabs-window>"), (
        "the switcher is inside the tab window, so switching means leaving the "
        "view you were reading to find it"
    )


def test_the_shell_hears_the_switch_so_a_chip_is_not_a_dead_tap():
    """Which run is selected is App.vue's state — the same state the fleet list and
    the drawer change — so the switch is a request that has to be forwarded twice
    and listened for once. A missing link anywhere on that path is a tap that does
    nothing, with no error and nothing on screen to say why."""
    component = _read(RELATED_RUNS)
    detail = _read(RUN_DETAIL)
    app = _read(APP)

    assert "defineEmits(['open'])" in component, "the switcher emits nothing"
    assert re.search(r"emit\('open', \{ host: entry\.host", component), (
        "the switcher emits something other than the run to open"
    )
    assert "'open'" in re.search(r"defineEmits\(\[([^\]]*)\]", detail).group(1), (
        "RunDetail.vue does not declare the forwarded event"
    )
    assert re.search(r"<RelatedRuns[^>]*@open=\"emit\('open', \$event\)\"", detail), (
        "RunDetail.vue does not forward the switch to the shell"
    )
    detail_tag = app[app.index("<RunDetail"):app.index("/>", app.index("<RunDetail"))]
    assert '@open="open"' in detail_tag, (
        "App.vue does not listen for the switch, so every related-run chip is a "
        f"tap that does nothing. It handles: {detail_tag}"
    )


def test_an_untracked_relation_is_a_key_with_a_hint_and_no_way_through():
    """The degradation the daemon's ``tracked`` flag exists for. The shell selects
    a run out of the fleet, so a "switch" to a run it does not carry would land on
    an empty page — and a chip that looks like the others and does nothing is worse
    than one that says why."""
    text = _read(RELATED_RUNS)

    assert re.search(r'v-if="entry\.tracked"', text), (
        "both cases render the same, so an untracked relation offers a dead switch"
    )
    switch = _function_source(text, "function switchTo")
    assert "if (!entry.tracked) return" in switch, (
        "the switch does not refuse an untracked run, so the markup is the only "
        "thing standing between a chip and a blank page"
    )
    assert ":title=\"NOT_HERE_HINT\"" in text, "the untracked chip explains nothing"
    hint = text[text.index("const NOT_HERE_HINT"):text.index("const ADD_HINT")]
    assert "not on this host" in hint and "Adopt" in hint, (
        "the hint does not say what is wrong or what to do about it"
    )
    # The key, because there is no row anywhere else in this app that names it.
    assert "{{ entry.key }}" in text


def test_the_switcher_reaches_the_daemon_through_the_api_client():
    """Every other view posts through api.js; this one does not get to be special."""
    client = _read(API_CLIENT)
    for call in (
        "export function fetchRunRelations(",
        "export function relateRun(",
        "export function unrelateRun(",
    ):
        assert call in client, f"api.js has no {call}"
    assert "api/runs/relations?" in client
    assert "request('api/runs/relate'" in client
    assert "request('api/runs/unrelate'" in client
    assert "'/api/runs/relate'" not in client, (
        "an absolute path breaks the UI behind a reverse proxy on a subpath"
    )

    text = _read(RELATED_RUNS)
    assert "from '../api.js'" in text
    assert "fetch(" not in text, "a hand-rolled request outside the one client"


def test_the_panel_says_the_relations_are_local_to_this_orchestrator():
    """Somebody will otherwise assume they travel with the run. On the page rather
    than in the API docs, because the page is where they get made."""
    text = _read(RELATED_RUNS).lower()

    assert "local to this orchestrator" in text
    assert "work repo" in text
    assert "forgetting a run does not remove them" in text


def test_the_panel_hardcodes_no_colour_and_no_swept_out_variant():
    """House style: the theme owns colour, and `outlined`/`flat` were swept out of
    every component — tonal and elevated are what this app uses."""
    text = _read(RELATED_RUNS)

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
    assert 'variant="outlined"' not in text
    assert 'variant="flat"' not in text
    # Icons are bundled SVG path data; the MDI webfont renders as empty boxes on a
    # LAN with no route out.
    assert "from '@mdi/js'" in text


def test_the_panel_stores_nothing_in_the_browser():
    """It needs no preference: it is always visible, and what it shows belongs to
    the daemon."""
    text = _read(RELATED_RUNS)

    assert "localStorage" not in text and "sessionStorage" not in text


def _function_source(text, signature):
    """Source of one top-level function in a ``<script setup>`` block.

    The same helper the other web guards use: every function in these components is
    top-level, so a ``}`` in column zero ends one.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]
