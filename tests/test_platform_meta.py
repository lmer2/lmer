"""What a run is about: title and description (issue #141, T52).

The operator asked for metadata the user can identify a run by, set by the
orchestrator when it starts a run and editable afterwards. The whole design turns
on one constraint, so most of what is pinned here is about *where the text lives*:

- **spec D3: the platform never writes run state.** Its copy of the work repo is
  a mirror the daemon force-resets on every pull, so metadata stored beside
  ``state.yaml`` would be destroyed by the next fetch with no error anywhere.
  This is the guard that would have caught the obvious implementation
- and the consequence of obeying D3, which is a real limitation and not an
  oversight: the metadata is **local to this orchestrator**, so it goes when the
  run is forgotten and it exists nowhere else
- a metadata write never touches ``runs.json``. That file decides which runs
  exist for this orchestrator, and the reason metadata got its own snapshot is
  that a lost update there costs a *run* rather than a title
- the text is agent-writable over the API, so it is bounded, stripped of control
  characters, and (for the title) collapsed to the one line it is displayed as
- the write route is reachable by anything holding the shared secret, which is
  how the orchestrator agent sets these — no channel of its own (T30), and since
  T88 a spawn can carry the title so that naming a run it starts is one call
  rather than two
- the UI renders the description through the shared markdown component and adds
  no second render path, and its tab is validated the way T49 validates the
  others: a remembered tab that no longer exists must fall back rather than
  render a blank run view

How the tab looks, and whether four of them crowd a phone, are verified by
building the bundle and by live test LT3 on a real phone.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import meta, runs, spawn, store
from tests.conftest import strip_lmer_env

# One depth-aware parser for the detail view's panels, imported rather than
# copied: the lmer panel *contains* a second tab window, and a flat split would
# hand its panes back as if they were top-level tabs.
from tests.test_platform_web_details_tabs import _panels

SECRET = "test-secret-value"

WEB = Path(__file__).resolve().parent.parent / "web"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
RUN_META = WEB / "src" / "components" / "RunMeta.vue"
API_CLIENT = WEB / "src" / "api.js"

RUN = ("gitlab.example.com", "agents/global", "develop-1")
BODY = {"host": RUN[0], "project": RUN[1], "slug": RUN[2]}


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
    """The normal precondition: a run this orchestrator already tracks."""
    runs.track(*RUN, source="spawned", taskdef="develop", target="issue-141")
    return RUN


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


#: An ``last_seen`` far enough in the past that any rewrite of the index changes
#: it. Without this the two guards below could pass vacuously: ``track()`` stamps
#: a whole-second timestamp, so a second call in the same second writes the same
#: bytes and a rewrite that should have been caught would not be.
AGED = "2026-01-01T00:00:00Z"


def _age_the_index():
    """Backdate the tracked entry, and hand back the index file's exact bytes."""
    index = runs.load_index()
    index[runs.run_key(*RUN)]["last_seen"] = AGED
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})
    return store.snapshot_path(runs.RUNS_FILE)


# --- where it lives, and why (spec D3) ---------------------------------------

def _mirror_contents(config):
    return {
        path: path.read_bytes()
        for path in sorted(config.mirror_path.rglob("*"))
        if path.is_file()
    }


def test_metadata_is_platform_state_and_never_reaches_the_work_repo(
    tracked, platform_root
):
    """The constraint the whole design comes from.

    The daemon's mirror is read-only and force-reset on every pull, so a title
    written into a run's directory survives exactly until the next fetch and then
    vanishes with no error anywhere — the worst available failure for a field
    whose job is to still be there tomorrow. A planted state.yaml is included
    because "wrote nothing new" and "left what was there alone" are different
    claims and both have to hold.
    """
    config = cfg.load()
    run_dir = config.mirror_path / RUN[0] / RUN[1] / "runs" / RUN[2]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\n", encoding="utf-8"
    )
    before = _mirror_contents(config)

    meta.write(*RUN, title="nightly review", description="why this run exists")

    assert _mirror_contents(config) == before, (
        "metadata reached the work-repo mirror, which the next pull destroys"
    )
    assert (platform_root / meta.META_FILE).is_file(), (
        "metadata is not in platform state either — where did it go?"
    )


def test_a_metadata_write_does_not_rewrite_the_run_index(tracked, platform_root):
    """The reason this is its own snapshot rather than two fields on runs.json.

    ``store`` gives each write atomicity but explicitly not consistency across a
    read-modify-write, and the API's handlers are synchronous so Starlette runs
    them concurrently in a threadpool. Sharing one file means a metadata write
    racing a spawn's ``track()`` can drop a tracked *run* — a row vanishing from
    the fleet — where separate files make the worst case a lost rename.
    """
    index = _age_the_index()
    before = index.read_bytes()

    meta.write(*RUN, title="nightly review")

    assert index.read_bytes() == before, (
        "a title edit rewrote the tracked-run index, so it can now lose a run"
    )
    assert store.snapshot_path(meta.META_FILE) != index


def test_renaming_a_run_does_not_make_it_look_recently_active(tracked, platform_root):
    """The other half of that decision, and the bug the cheap version has.

    Editing a title through ``track()`` would refresh ``last_seen``, which is what
    ``list_tracked`` orders by — so renaming a finished run would shove it to the
    top of the fleet as if it had just done something.
    """
    _age_the_index()

    meta.write(*RUN, title="a name I gave it much later")

    assert runs.get_tracked(*RUN).last_seen == AGED, (
        "describing a run marked it as active, so renaming a finished one sends "
        "it back to the top of the fleet"
    )


def test_the_snapshot_is_human_readable(tracked, platform_root):
    """D2's point: repairable in an editor when the orchestrator is wedged."""
    meta.write(*RUN, title="nightly review")
    payload = json.loads(_read(store.snapshot_path(meta.META_FILE)))

    assert payload["meta"]["gitlab.example.com/agents/global/develop-1"]["title"] == (
        "nightly review"
    )
    assert payload["schema"] == store.SCHEMA_VERSION


# --- reading and writing ------------------------------------------------------

def test_a_run_nobody_has_described_reads_as_empty(tracked, platform_root):
    """Empty rather than ``None``: it is the common case, and making it the
    absent-value case would put a branch in every consumer."""
    record = meta.read(*RUN)

    assert record.title == "" and record.description == ""
    assert record.updated_at is None
    assert record.empty is True


def test_a_title_and_description_round_trip(tracked, platform_root):
    stored = meta.write(*RUN, title="nightly review", description="the **why**")

    assert stored.title == "nightly review"
    assert stored.description == "the **why**"
    assert stored.updated_at, "nothing records when this was written"
    assert stored.empty is False
    assert meta.read(*RUN).to_dict() == stored.to_dict()


def test_setting_one_field_leaves_the_other_alone(tracked, platform_root):
    """The orchestrator agent setting a title must not silently delete a
    description an operator wrote."""
    meta.write(*RUN, title="first", description="a description somebody wrote")
    meta.write(*RUN, title="second")

    record = meta.read(*RUN)
    assert record.title == "second"
    assert record.description == "a description somebody wrote"


def test_an_empty_string_clears_a_field_and_an_omission_does_not(
    tracked, platform_root
):
    meta.write(*RUN, title="first", description="kept")
    meta.write(*RUN, title="")

    record = meta.read(*RUN)
    assert record.title == ""
    assert record.description == "kept"


def test_clearing_both_fields_removes_the_entry_rather_than_leaving_a_husk(
    tracked, platform_root
):
    """"Cleared" and "never set" have to be the same state, or a run reads as
    described when the only thing left is a timestamp."""
    meta.write(*RUN, title="gone", description="also gone")
    cleared = meta.write(*RUN, title="", description="")

    assert cleared.empty is True
    assert cleared.updated_at is None
    assert meta.load_all() == {}


def test_setting_nothing_at_all_is_refused(tracked, platform_root):
    """A write that names no field is a caller bug, not a no-op — silently
    answering 200 would let a client believe it had cleared something."""
    with pytest.raises(meta.MetaError) as refusal:
        meta.write(*RUN)

    assert refusal.value.status == 400
    assert "nothing to set" in str(refusal.value)


def test_two_runs_keep_their_own_metadata(platform_root):
    runs.track("h", "p", "a")
    runs.track("h", "p", "b")
    meta.write("h", "p", "a", title="the first one")
    meta.write("h", "p", "b", title="the second one")

    assert meta.read("h", "p", "a").title == "the first one"
    assert meta.read("h", "p", "b").title == "the second one"


# --- the text is untrusted ----------------------------------------------------

def test_a_title_is_collapsed_to_one_line(tracked, platform_root):
    """It is a label in a list, and the daemon is what makes that true — a client
    that trusted the text it sent would render three lines where one fits."""
    stored = meta.write(*RUN, title="  a title\nwith  a newline\tin it  ")

    assert stored.title == "a title with a newline in it"


def test_a_description_keeps_its_shape_because_it_is_markdown(tracked, platform_root):
    """The opposite rule to the title's, and for a stated reason: paragraphs and
    list items are the formatting, so collapsing them would destroy the content."""
    stored = meta.write(*RUN, description="- one\n- two\n\nand a paragraph\n\n\n")

    assert stored.description == "- one\n- two\n\nand a paragraph"


@pytest.mark.parametrize("field", ["title", "description"])
def test_control_characters_are_stripped(tracked, platform_root, field):
    """No reading of this text has a NUL or a bell in it on purpose, and it lands
    in a JSON file an operator opens in an editor as well as in a browser."""
    stored = meta.write(*RUN, **{field: "clean\x00er\x07 than\x1b it looks"})

    assert getattr(stored, field) == "cleaner than it looks"


def test_a_lone_carriage_return_cannot_survive_in_a_description(tracked, platform_root):
    """A CR overwrites the line before it wherever this text is echoed, which is
    how "line one" becomes invisible in a terminal reading the state file."""
    stored = meta.write(*RUN, description="line one\r\nline two\rnot an overwrite")

    assert stored.description == "line one\nline two\nnot an overwrite"


@pytest.mark.parametrize(
    "field,limit",
    [("title", meta.MAX_TITLE_CHARS), ("description", meta.MAX_DESCRIPTION_CHARS)],
)
def test_a_field_over_its_bound_is_refused_with_both_numbers(
    tracked, platform_root, field, limit
):
    """Bounded rather than trusted to be reasonable: this is typed into a browser
    or written by an agent, and it ends up in a file. The refusal carries the size
    and the limit because the caller's next move depends on the difference."""
    with pytest.raises(meta.MetaError) as refusal:
        meta.write(*RUN, **{field: "x" * (limit + 1)})

    assert refusal.value.status == 400
    assert str(limit) in str(refusal.value) and str(limit + 1) in str(refusal.value)
    assert meta.read(*RUN).empty, "a refused write stored something anyway"


def test_the_bound_is_checked_after_the_text_is_cleaned(tracked, platform_root):
    """Otherwise a title that is only over the line because it was pasted with
    hard wraps is refused for the whitespace that was about to be removed."""
    padded = "  ".join("word" for _ in range(24)) + "   "
    assert len(padded) > meta.MAX_TITLE_CHARS, (
        "the fixture is under the bound either way, so this asserts nothing"
    )

    stored = meta.write(*RUN, title=padded)

    assert len(stored.title) <= meta.MAX_TITLE_CHARS
    assert "  " not in stored.title


@pytest.mark.parametrize("value", [17, ["a list"], {"a": "mapping"}, True])
def test_a_field_that_is_not_text_is_refused(tracked, platform_root, value):
    """Not coerced: ``str(value)`` would store "17" and "True" as somebody's
    considered description of a run."""
    with pytest.raises(meta.MetaError, match="must be text"):
        meta.write(*RUN, title=value)


def test_the_stored_text_is_never_logged(tracked, platform_root, caplog):
    """Lengths, not content. This is agent-authored text of arbitrary shape and a
    log line is not where it belongs."""
    caplog.set_level("INFO")
    meta.write(*RUN, title="a secret-looking title", description="body text")

    written = " ".join(record.message for record in caplog.records)
    assert "platform_run_meta_set" in written
    assert "a secret-looking title" not in written
    assert "body text" not in written


# --- scope: a note about a run in THIS fleet ----------------------------------

def test_describing_an_untracked_run_is_refused_as_a_404(platform_root):
    """Metadata is scoped to a tracked run. Attaching one to anything else makes
    state that no view lists and no forget can reach, which is a leak."""
    with pytest.raises(meta.RunNotTracked) as refusal:
        meta.write("h", "p", "never-tracked", title="orphan")

    assert refusal.value.status == 404
    assert "adopt" in str(refusal.value).lower(), "the refusal names no way through"
    assert meta.load_all() == {}


def test_reading_an_untracked_run_is_empty_rather_than_an_error(platform_root):
    """The read is deliberately quieter than the write: the caller asked what is
    recorded and "nothing" is true, while a refusal would turn a run forgotten in
    another tab into an error page."""
    assert meta.read("h", "p", "never-tracked").empty is True


def test_forgetting_a_run_takes_its_metadata_with_it(tracked, platform_root):
    """The consequence of D3 that has to be stated: this text is local to this
    orchestrator, so the verb that ends the tracking ends the note too."""
    meta.write(*RUN, title="nightly review", description="the why")

    assert runs.forget(*RUN) is True
    assert meta.read(*RUN).empty is True
    assert meta.load_all() == {}


def test_forgetting_one_run_leaves_another_runs_metadata_alone(platform_root):
    runs.track("h", "p", "keep")
    runs.track("h", "p", "drop")
    meta.write("h", "p", "keep", title="kept")
    meta.write("h", "p", "drop", title="dropped")

    runs.forget("h", "p", "drop")

    assert meta.read("h", "p", "keep").title == "kept"
    assert meta.read("h", "p", "drop").empty is True


def test_forgetting_a_run_that_was_never_described_is_not_an_error(
    tracked, platform_root
):
    assert runs.forget(*RUN) is True


def test_a_failure_to_drop_metadata_does_not_fail_the_forget(
    tracked, platform_root, caplog
):
    """The run *is* forgotten by the time the cleanup runs, so raising there would
    report a failure of something that succeeded. An orphan is worth a log line."""
    meta.write(*RUN, title="nightly review")

    def refuse(*args, **kwargs):
        raise store.StoreError("disk is full")

    original = meta.drop
    meta.drop = refuse
    try:
        assert runs.forget(*RUN) is True
    finally:
        meta.drop = original

    assert runs.get_tracked(*RUN) is None, "the run survived its own forget"
    assert any("platform_run_meta_orphaned" in r.message for r in caplog.records)


# --- the other writer: a spawn that names its run (T88) -----------------------
#
# The operator asked for a run to be titled when it is *started*, which makes
# :mod:`lmer_platform.spawn` the second caller of ``write`` — and the interesting
# one, because it calls from a place where nothing can be undone: the container is
# already running, the session is registered and the run is tracked. So the spawn
# side owns an ordering (below) while this module keeps every rule about the text.
# What the whole spawn does with it is pinned in ``tests/test_platform_addrun.py``,
# beside the fields that are argv; these are the two claims that belong to this
# file.

def test_a_spawn_writes_through_this_module_rather_than_beside_it(
    tracked, platform_root
):
    """One field, one file, one set of rules — whichever set it.

    A spawn-time title and an operator's later rename have to be the same state, or
    the tab would show something the fleet row does not. Handing both to ``write``
    is what makes that structural instead of a coincidence, and the collapse is the
    visible proof it happened: nothing on the spawn side knows about it.
    """
    warning = spawn._write_run_meta(
        spawn.SpawnRequest(
            taskdef="develop", target="issue-141",
            title="  two\nlines  ", description="the **why**\n\n\n",
        ),
        *RUN,
    )

    assert warning is None
    stored = meta.read(*RUN)
    assert stored.title == "two lines"
    assert stored.description == "the **why**"
    assert list(meta.load_all()) == [runs.run_key(*RUN)]


@pytest.mark.parametrize("run, refusal", [
    (RUN, "characters"),
    (("h", "p", "never-tracked"), "not tracked"),
])
def test_a_refusal_reaches_a_spawn_as_a_warning_and_never_as_an_exception(
    tracked, platform_root, run, refusal
):
    """The same trade :func:`lmer_platform.runs.forget` makes with ``drop``.

    Both refusals this module can give are reachable from a spawn — an agent's
    over-long title, and a run that stopped being tracked between the ``track``
    call and this one — and both arrive after the session exists. Raising would
    report a failed spawn for a container that is running, with nothing to undo it
    with, so the refusal comes back as text the caller can pass on.
    """
    request = spawn.SpawnRequest(
        taskdef="develop", target="issue-141",
        title="x" * (meta.MAX_TITLE_CHARS + 1) if refusal == "characters" else "name",
    )

    warning = spawn._write_run_meta(request, *run)

    assert refusal in warning
    assert "POST /api/runs/meta" in warning, "the warning names no way to fix it"
    assert meta.load_all() == {}


# --- durability ---------------------------------------------------------------

def test_a_corrupt_snapshot_reads_as_empty_and_is_logged(
    tracked, platform_root, caplog
):
    """One bad file must not take a run's detail view down with it, and the bad
    bytes are the only record of what went wrong — so they are moved aside."""
    path = store.snapshot_path(meta.META_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert meta.read(*RUN).empty is True
    assert any("platform_run_meta_unreadable" in r.message for r in caplog.records)
    assert len(list(path.parent.glob("run_meta.json.bad-*"))) == 1


def test_a_malformed_entry_is_skipped_rather_than_fatal(tracked, platform_root, caplog):
    """Hand-edited, or written by a version that changed shape."""
    store.write_json(
        store.snapshot_path(meta.META_FILE),
        {"meta": {"gitlab.example.com/agents/global/develop-1": "not a mapping"}},
    )

    assert meta.read(*RUN).empty is True
    assert any("platform_run_meta_malformed" in r.message for r in caplog.records)


def test_a_field_of_the_wrong_type_on_disk_reads_as_empty(tracked, platform_root):
    store.write_json(
        store.snapshot_path(meta.META_FILE),
        {"meta": {"gitlab.example.com/agents/global/develop-1": {"title": 17}}},
    )

    assert meta.read(*RUN).title == ""


def test_a_snapshot_without_a_meta_key_reads_as_empty(tracked, platform_root):
    store.write_json(store.snapshot_path(meta.META_FILE), {"something": "else"})

    assert meta.read(*RUN).empty is True


# --- over the API --------------------------------------------------------------

def test_the_meta_routes_require_the_secret(client):
    assert client.get("/api/runs/meta").status_code == 401
    assert client.post("/api/runs/meta", json=BODY).status_code == 401


def test_reading_and_writing_a_runs_metadata_over_the_api(client, tracked):
    written = client.post(
        "/api/runs/meta", headers=bearer_header(),
        json={**BODY, "title": "nightly review", "description": "the **why**"},
    )
    assert written.status_code == 200
    assert written.json()["meta"]["title"] == "nightly review"

    read = client.get(
        "/api/runs/meta",
        headers=bearer_header(),
        params=BODY,
    )
    assert read.status_code == 200
    assert read.json()["meta"] == written.json()["meta"]
    assert read.json()["run"] == BODY


def test_the_write_route_is_reachable_with_the_shared_secret_alone(client, tracked):
    """How the orchestrator agent sets these (T30): the same bearer credential
    every other client uses, over the same REST surface. Nothing about this
    feature needed a channel of its own, and this is the assertion that says so —
    a header, a body, and no session, ticket or container anywhere in it.
    """
    response = client.post(
        "/api/runs/meta",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={**BODY, "title": "set by the orchestrator"},
    )

    assert response.status_code == 200
    assert meta.read(*RUN).title == "set by the orchestrator"


def test_the_reply_carries_the_bounds_the_daemon_will_enforce(client, tracked):
    """So a form's counter and the server's refusal cannot drift apart."""
    payload = client.get("/api/runs/meta", headers=bearer_header(), params=BODY).json()

    assert payload["limits"] == {
        "title": meta.MAX_TITLE_CHARS,
        "description": meta.MAX_DESCRIPTION_CHARS,
    }


def test_every_reply_says_the_metadata_is_local_to_this_orchestrator(client, tracked):
    """The surprising half of the feature, and the one a client would otherwise
    assume the other way round: this does not travel with the run."""
    payload = client.get("/api/runs/meta", headers=bearer_header(), params=BODY).json()

    assert payload["local"] is True
    note = payload["note"].lower()
    assert "platform state" in note and "work repo" in note
    assert "forgotten" in note, "nothing says the note dies with the run"


def test_the_reply_is_what_was_stored_and_not_what_was_sent(client, tracked):
    """A caller that just set a title should not have to re-read to learn what a
    bounded, collapsed field actually became."""
    payload = client.post(
        "/api/runs/meta", headers=bearer_header(),
        json={**BODY, "title": "two\nlines"},
    ).json()

    assert payload["meta"]["title"] == "two lines"


def test_describing_an_untracked_run_over_the_api_is_a_404(client, platform_root):
    response = client.post(
        "/api/runs/meta", headers=bearer_header(),
        json={"host": "h", "project": "p", "slug": "nope", "title": "orphan"},
    )

    assert response.status_code == 404
    assert "not tracked" in response.json()["detail"]


def test_an_over_long_field_over_the_api_is_a_400(client, tracked):
    response = client.post(
        "/api/runs/meta", headers=bearer_header(),
        json={**BODY, "title": "x" * (meta.MAX_TITLE_CHARS + 1)},
    )

    assert response.status_code == 400
    assert str(meta.MAX_TITLE_CHARS) in response.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"host": "h"}, {"host": "h", "project": "p"}])
def test_an_incomplete_run_reference_is_a_400_that_names_the_field(client, body):
    """The same refusal ``/api/runs/adopt`` gives, rather than a validation error
    in a different shape for the same mistake."""
    response = client.post(
        "/api/runs/meta", headers=bearer_header(), json={**body, "title": "x"},
    )

    assert response.status_code == 400
    assert "non-empty string" in response.json()["detail"]


def test_a_read_with_no_run_named_is_a_400_rather_than_a_validation_error(client):
    response = client.get("/api/runs/meta", headers=bearer_header())

    assert response.status_code == 400
    assert "host" in response.json()["detail"]


def test_the_route_list_documents_both_verbs_and_the_locality(client):
    body = client.get("/api", headers=bearer_header()).text

    assert "GET  /api/runs/meta" in body
    assert "POST /api/runs/meta" in body
    assert "never the work repo" in body, (
        "the route list does not say where this state lives, which is the one "
        "thing a client would guess wrong"
    )


# --- the UI ---------------------------------------------------------------------

def test_the_detail_view_has_a_meta_tab_holding_the_new_component():
    detail = _read(RUN_DETAIL)
    panels = _panels("tab")

    assert "meta" in panels, f"the top-level tabs are {sorted(panels)}"
    assert '<v-tab value="meta">' in detail, "no tab selects the meta panel"
    assert "<RunMeta" in panels["meta"], "the meta tab renders something else"
    assert "import RunMeta from './RunMeta.vue'" in detail


def test_every_tab_is_rememberable_and_every_remembered_tab_has_a_panel():
    """The invariant T49's validation rests on, restated so adding a tab cannot
    break it silently.

    Vuetify renders no panel for a model value that selects none, so a tab missing
    from TABS is one that resets on every visit, and a TABS entry with no panel is
    a *blank run view* for anyone whose browser remembered it. Neither has a
    symptom in a build.
    """
    detail = _read(RUN_DETAIL)
    declared = re.search(r"const TABS = \[([^\]]*)\]", detail)
    assert declared, "RunDetail.vue no longer declares its tab list"
    remembered = re.findall(r"'([\w-]+)'", declared.group(1))

    bar = detail.index('<v-tabs v-model="tab"')
    selectable = re.findall(
        r'<v-tab value="([\w-]+)"', detail[bar:detail.index("</v-tabs>", bar)],
    )

    assert set(remembered) == set(_panels("tab")) == set(selectable), (
        f"remembered {sorted(remembered)}, panels {sorted(_panels('tab'))}, "
        f"tabs {sorted(selectable)}"
    )
    assert remembered[0] == "overview", (
        "the first entry is the fallback for a remembered tab this build does not "
        f"have, and it is {remembered[0]!r}"
    )


def test_the_description_goes_through_the_shared_renderer():
    """Agent-authored text becoming markup, which is the one thing this app does
    in exactly one place. A second render path here — or a v-html — is the hole
    Markdown.vue exists to make impossible."""
    text = _read(RUN_META)

    # Deferred since T42 (see tests/test_platform_web_bundle.py): the renderer is a
    # chunk of its own, so this is defineAsyncComponent and not a static import.
    # RunMeta was in fact the consumer that kept it in the initial bundle.
    assert "defineAsyncComponent" in text and "import('./Markdown.vue')" in text
    assert re.search(r'<Markdown\s+[^>]*:text="record\.description"', text), (
        "the description is not rendered through the shared component"
    )
    assert "v-html" not in text
    assert "markdown-it" not in text and "dompurify" not in text, (
        "a second renderer, which is how one view ends up without the sanitiser"
    )


def test_the_title_is_shown_as_text_and_never_rendered():
    """It is a label the daemon already collapsed to one line. Rendered, a stray
    pair of asterisks would make a tab heading pretend to be a document."""
    text = _read(RUN_META)

    assert "{{ record.title }}" in text
    assert not re.search(r"<Markdown[^>]*record\.title", text)


def test_the_panel_says_the_note_is_local_to_this_orchestrator():
    """Somebody will otherwise assume it syncs. It is on the page rather than in
    the API docs because the page is where the text gets typed."""
    text = _read(RUN_META).lower()

    assert "local to this orchestrator" in text
    assert "work repo" in text
    assert "forget the run" in text


def test_the_bounds_shown_to_the_operator_come_from_the_daemon():
    """A maxlength this view invented would be a second copy of a number that
    already exists, and the two would drift the first time one changed."""
    text = _read(RUN_META)

    assert 'reply.limits' in text, "the bounds are not read off the reply"
    assert ':maxlength="limits.title"' in text
    assert ':maxlength="limits.description"' in text
    assert not re.search(r'maxlength="\d+"', text), "a hardcoded bound"


def test_the_meta_view_reaches_the_daemon_through_the_api_client():
    """Every other view posts through api.js; this one does not get to be special."""
    client = _read(API_CLIENT)
    assert "export function fetchRunMeta(" in client
    assert "export function setRunMeta(" in client
    assert "request('api/runs/meta'" in client and "api/runs/meta?" in client
    assert "'/api/runs/meta'" not in client, (
        "an absolute path breaks the UI behind a reverse proxy on a subpath"
    )

    text = _read(RUN_META)
    assert "from '../api.js'" in text
    assert "fetch(" not in text, "a hand-rolled request outside the one client"


def test_the_meta_panel_hardcodes_no_colour_and_no_swept_out_variant():
    """House style: the theme owns colour, and `outlined`/`flat` were swept out of
    every component — tonal and elevated are what this app uses."""
    text = _read(RUN_META)

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
    assert 'variant="outlined"' not in text
    assert 'variant="flat"' not in text
    # Icons are bundled SVG path data; the MDI webfont renders as empty boxes on a
    # LAN with no route out.
    assert "from '@mdi/js'" in text


def test_the_meta_panel_stores_nothing_in_the_browser():
    """It needs no preference: which tab you read is already remembered one level
    up, and the text itself belongs to the daemon."""
    text = _read(RUN_META)

    assert "localStorage" not in text and "sessionStorage" not in text
