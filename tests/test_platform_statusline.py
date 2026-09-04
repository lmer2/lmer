"""The platform's end of a session's status document (issue #248).

The reading half; ``tests/test_statusline.py`` owns the writing half and pins
the round trip. Almost all of what is kept here is the two properties that make
a strip safe to put on the one payload the whole app polls: **absence is an
answer** (no document, an older image, a torn read, a harness that reports
nothing — all of it reads as "no status" and none of it raises), and **the
document is untrusted input** (written in a container, on every render, and it
lands in an HTTP response — so bounded, clamped, and a newer format refused).

The window vocabulary is deliberately not pinned to claude's, so a document
whose windows no lmer build has heard of is read here too.
"""

import json
import os
import stat
import threading
from datetime import datetime, timezone

import pytest

from lmer_platform import registry, statusline, store
from tests.conftest import strip_lmer_env

SESSION = "s-20260903-141530-abcd"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _document(**overrides):
    document = {
        "schema": 1,
        "harness": "claude",
        "updated": "2026-09-03T14:31:07Z",
        "context": {"percent": 9},
        "limits": [
            {"key": "five_hour", "label": "5h", "percent": 24,
             "resets_at": 1738425600},
            {"key": "seven_day", "label": "7d", "percent": 41},
        ],
        "effort": {"level": "high"},
    }
    document.update(overrides)
    return document


def _publish(session_id=SESSION, document=None, raw=None):
    """Put a document where the session would have published it."""
    directory = statusline.prepare_status_dir(session_id)
    assert directory is not None
    path = directory / statusline.STATUS_FILENAME
    path.write_text(raw if raw is not None else json.dumps(
        _document() if document is None else document
    ))
    return path


def _now(stamp="2026-09-03T14:31:10Z"):
    return datetime.strptime(stamp, store.TS_FORMAT).replace(tzinfo=timezone.utc)


class TestLayout:
    def test_the_directory_sits_beside_the_other_per_session_state(self, platform_root):
        directory = statusline.session_status_dir(SESSION)
        assert directory == store.logs_dir() / f"{SESSION}.status"
        assert statusline.status_file_for(SESSION) == directory / "status.json"

    def test_a_session_id_that_could_not_name_a_session_is_refused(self, platform_root):
        """The registry's notion of a legal id, borrowed rather than written
        again: two notions is how ``..`` gets past one of them."""
        with pytest.raises(registry.RegistryError):
            statusline.session_status_dir("../../etc")

    def test_a_refused_id_reads_as_no_status_rather_than_raising(self, platform_root):
        """The read path is reachable from an HTTP route, so it answers the
        same "no" it answers for every other unusable document."""
        assert statusline.for_session("../../etc") is None


class TestPrepareStatusDir:
    def test_creates_it_owner_only(self, platform_root):
        directory = statusline.prepare_status_dir(SESSION)
        assert directory is not None and directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_is_idempotent_and_corrects_a_wide_mode(self, platform_root):
        directory = statusline.prepare_status_dir(SESSION)
        directory.chmod(0o755)
        again = statusline.prepare_status_dir(SESSION)
        assert again == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_an_unusable_location_is_none_not_a_refused_spawn(self, platform_root):
        """A session without this directory publishes nothing and shows no
        strip — which is every session that ran before the feature existed, and
        not worth refusing to start one over."""
        logs = store.logs_dir()
        logs.parent.mkdir(parents=True, exist_ok=True)
        logs.write_text("not a directory")
        assert statusline.prepare_status_dir(SESSION) is None

    def test_a_rejected_id_is_none(self, platform_root):
        assert statusline.prepare_status_dir("../evil") is None


class TestDiscardStatusDir:
    def test_removes_the_document_and_the_directory(self, platform_root):
        path = _publish()
        statusline.discard_status_dir(SESSION)
        assert not path.exists()
        assert not path.parent.exists()

    def test_nothing_to_remove_is_not_an_error(self, platform_root):
        statusline.discard_status_dir(SESSION)
        statusline.discard_status_dir("../evil")

    def test_leaves_a_directory_holding_anything_else(self, platform_root):
        """Cleanup that emptied a directory it did not fill would delete
        whatever a future writer put beside the document."""
        directory = statusline.prepare_status_dir(SESSION)
        (directory / "something-else").write_text("x")
        statusline.discard_status_dir(SESSION)
        assert directory.is_dir()


class TestReadDocument:
    def test_the_whole_document(self, platform_root):
        served = statusline.read_document(_publish(), now=_now())
        assert served == {
            "harness": "claude",
            "updated": "2026-09-03T14:31:07Z",
            "age_seconds": 3.0,
            "context": {"percent": 9},
            "limits": [
                {"key": "five_hour", "label": "5h", "percent": 24,
                 "resets_at": "2025-02-01T16:00:00Z"},
                {"key": "seven_day", "label": "7d", "percent": 41},
            ],
            "effort": {"level": "high"},
        }

    def test_absent_is_no_status(self, platform_root):
        assert statusline.read_document(store.logs_dir() / "nope.json") is None

    def test_a_directory_where_the_document_should_be(self, platform_root):
        directory = statusline.prepare_status_dir(SESSION)
        assert statusline.read_document(directory) is None

    @pytest.mark.parametrize(
        "raw",
        ['{"schema": 1, "harness": "cla',   # torn mid-write
         "",                                # created but not yet written
         "not json at all",
         "[1, 2, 3]",                       # JSON, but not an object
         '"just a string"'],
    )
    def test_unusable_content_is_no_status(self, platform_root, raw):
        assert statusline.read_document(_publish(raw=raw)) is None

    def test_a_fifo_is_refused_instead_of_blocking_forever(self, platform_root):
        """The one shape a ``stat`` + ``read_text`` does not cover, and the worst
        one: a FIFO passes the size cap (``st_size == 0``) and then blocks the
        read forever — on the fleet payload's own path, so ``/api/state`` stops
        answering for every viewer. Reproduced on this module's first cut; this
        test is that repro, bounded.
        """
        directory = statusline.prepare_status_dir(SESSION)
        os.mkfifo(directory / statusline.STATUS_FILENAME)
        finished = []

        def read():
            finished.append(statusline.for_session(SESSION))

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "the read blocked on a FIFO"
        assert finished == [None]

    def test_a_symlink_at_that_name_is_not_followed(self, platform_root, tmp_path):
        """The directory is container-writable, so the name can be a symlink out
        of it by the time this opens — ``O_NOFOLLOW`` is what stops one session
        aiming the daemon's read at an arbitrary file."""
        outside = tmp_path / "elsewhere.json"
        outside.write_text(json.dumps(_document()))
        directory = statusline.prepare_status_dir(SESSION)
        (directory / statusline.STATUS_FILENAME).symlink_to(outside)
        assert statusline.for_session(SESSION) is None

    def test_a_document_larger_than_the_cap_is_refused_unread(self, platform_root):
        path = _publish(raw=json.dumps(
            {"schema": 1, "harness": "claude", "pad": "x" * statusline.MAX_DOCUMENT_BYTES}
        ))
        assert path.stat().st_size > statusline.MAX_DOCUMENT_BYTES
        assert statusline.read_document(path) is None

    @pytest.mark.parametrize("schema", [2, 99])
    def test_a_newer_format_is_refused_rather_than_half_read(self, platform_root, schema):
        """The two halves ship independently, so a document this build does not
        know the shape of is read as nothing — the same rule the transcript
        format's canonical reader keeps."""
        assert statusline.read_document(_publish(document=_document(schema=schema))) is None

    @pytest.mark.parametrize("schema", [None, "1", 1.0, True])
    def test_a_document_that_does_not_declare_an_integer_schema(self, platform_root, schema):
        assert statusline.read_document(_publish(document=_document(schema=schema))) is None

    def test_an_older_schema_is_still_read(self, platform_root):
        """Refusing a *newer* format is the rule; an older one is a document
        this build knows more than enough about."""
        served = statusline.read_document(_publish(document=_document(schema=0)))
        assert served is not None and served["context"] == {"percent": 9}

    def test_a_session_that_has_nothing_to_report_yet(self, platform_root):
        """The three identifying fields with no facts behind them: the document
        exists, so the session publishes — it just has nothing yet."""
        served = statusline.read_document(_publish(document={
            "schema": 1, "harness": "claude", "updated": "2026-09-03T14:31:07Z",
        }), now=_now())
        assert served == {
            "harness": "claude",
            "updated": "2026-09-03T14:31:07Z",
            "age_seconds": 3.0,
        }

    @pytest.mark.parametrize(
        "updated", ["last tuesday", "", "   ", None, 1738425600, [],
                    "2026-13-45T99:99:99Z", "x" * 200],
    )
    def test_a_reading_that_cannot_be_dated_is_not_served(
        self, platform_root, updated,
    ):
        """The whole document, not just its age: an undateable ``ctx 95%``
        renders exactly like a reading three seconds old, forever, for a harness
        that has stopped publishing.
        """
        assert statusline.read_document(
            _publish(document=_document(updated=updated)), now=_now()
        ) is None

    def test_a_missing_timestamp_is_not_served_either(self, platform_root):
        document = _document()
        del document["updated"]
        assert statusline.read_document(
            _publish(document=document), now=_now()
        ) is None

    @pytest.mark.parametrize(
        "written,normalised",
        [
            # What hooks/statusline.py writes.
            ("2026-09-03T14:31:07Z", "2026-09-03T14:31:07Z"),
            # What a drop-in's `datetime.now(timezone.utc).isoformat()` writes —
            # the spelling docs/SESSION-STATUS.md invites, which the strict
            # format rejects.
            ("2026-09-03T14:31:07.123456+00:00", "2026-09-03T14:31:07Z"),
            ("2026-09-03T14:31:07+00:00", "2026-09-03T14:31:07Z"),
            # Another zone, and no zone at all (documented as UTC).
            ("2026-09-03T16:31:07+02:00", "2026-09-03T14:31:07Z"),
            ("2026-09-03T14:31:07", "2026-09-03T14:31:07Z"),
        ],
    )
    def test_iso_8601_is_accepted_and_normalised(
        self, platform_root, written, normalised,
    ):
        """Wider on the way in than on the way out: a reading is not worth
        discarding over a punctuation difference, and one shape crosses the
        wire."""
        served = statusline.read_document(
            _publish(document=_document(updated=written)), now=_now(),
        )
        assert served["updated"] == normalised
        assert served["age_seconds"] == 3.0


class TestBounds:
    @pytest.mark.parametrize(
        "value,expected",
        [(23.5, 24), (0, 0), (100, 100), (250, 100), (-5, 0)],
    )
    def test_percentages_are_rounded_and_clamped(self, platform_root, value, expected):
        served = statusline.read_document(_publish(
            document=_document(context={"percent": value})
        ))
        assert served["context"] == {"percent": expected}

    @pytest.mark.parametrize("value", [None, "9", True, float("nan"), float("inf"), {}])
    def test_an_unusable_percentage_drops_the_block(self, platform_root, value):
        served = statusline.read_document(_publish(
            document=_document(context={"percent": value})
        ))
        assert "context" not in served

    def test_a_context_block_of_the_wrong_type_drops(self, platform_root):
        served = statusline.read_document(_publish(document=_document(context="9%")))
        assert "context" not in served

    def test_the_window_list_is_length_capped(self, platform_root):
        windows = [
            {"key": f"w{index}", "label": f"w{index}", "percent": index}
            for index in range(statusline.MAX_LIMITS + 5)
        ]
        served = statusline.read_document(_publish(document=_document(limits=windows)))
        assert len(served["limits"]) == statusline.MAX_LIMITS

    def test_names_are_clamped(self, platform_root):
        served = statusline.read_document(_publish(document=_document(
            limits=[{"key": "k" * 200, "label": "l" * 200, "percent": 5}],
            effort={"level": "e" * 200},
        )))
        assert len(served["limits"][0]["key"]) == statusline.MAX_NAME_CHARS
        assert len(served["limits"][0]["label"]) == statusline.MAX_NAME_CHARS
        assert len(served["effort"]["level"]) == statusline.MAX_NAME_CHARS

    @pytest.mark.parametrize(
        "window",
        [{"label": "5h", "percent": 24},          # no key
         {"key": "five_hour"},                    # no percentage
         {"key": "five_hour", "percent": "24"},   # unusable percentage
         {"key": "", "percent": 24},              # empty key
         "five_hour",                             # not an object
         None],
    )
    def test_a_window_nobody_can_name_or_measure_is_dropped(self, platform_root, window):
        served = statusline.read_document(_publish(document=_document(limits=[window])))
        assert "limits" not in served

    def test_duplicate_windows_are_dropped_after_the_first(self, platform_root):
        """The key is what a client draws the list by, so two entries sharing one
        would be two nodes with the same identity in the same list."""
        served = statusline.read_document(_publish(document=_document(limits=[
            {"key": "five_hour", "label": "5h", "percent": 24},
            {"key": "five_hour", "label": "5h again", "percent": 99},
        ])))
        assert served["limits"] == [
            {"key": "five_hour", "label": "5h", "percent": 24},
        ]

    def test_a_window_without_a_label_is_named_by_its_key(self, platform_root):
        served = statusline.read_document(_publish(
            document=_document(limits=[{"key": "five_hour", "percent": 24}])
        ))
        assert served["limits"] == [
            {"key": "five_hour", "label": "five_hour", "percent": 24},
        ]

    @pytest.mark.parametrize("value", [0, -1, "1738425600", True, None, float("inf")])
    def test_an_unusable_reset_is_absent_rather_than_1970(self, platform_root, value):
        served = statusline.read_document(_publish(document=_document(
            limits=[{"key": "five_hour", "label": "5h", "percent": 24,
                     "resets_at": value}]
        )))
        assert "resets_at" not in served["limits"][0]

    def test_a_limits_block_of_the_wrong_type_drops(self, platform_root):
        served = statusline.read_document(_publish(document=_document(limits={})))
        assert "limits" not in served

    def test_an_effort_block_of_the_wrong_type_drops(self, platform_root):
        served = statusline.read_document(_publish(document=_document(effort="high")))
        assert "effort" not in served

    def test_a_harness_this_build_never_heard_of(self, platform_root):
        """The whole point of the shape: windows name themselves, so a provider
        with its own vocabulary reads without a change here."""
        served = statusline.read_document(_publish(document={
            "schema": 1, "harness": "opencode",
            "updated": "2026-09-03T14:31:07Z",
            "limits": [{"key": "monthly_quota", "label": "mo", "percent": 12}],
            "effort": {"level": "thorough"},
        }))
        assert served["harness"] == "opencode"
        assert served["limits"] == [
            {"key": "monthly_quota", "label": "mo", "percent": 12},
        ]
        assert served["effort"] == {"level": "thorough"}


class TestBySession:
    def test_maps_live_sessions_to_what_they_published(self, platform_root):
        _publish()
        statuses = statusline.by_session([{"id": SESSION, "live": True}], now=_now())
        assert statuses[SESSION]["context"] == {"percent": 9}

    def test_a_dead_session_is_not_reported(self, platform_root):
        """A document outliving its session describes nobody: it is a number
        whose subject stopped rendering."""
        _publish()
        assert statusline.by_session([{"id": SESSION, "live": False}]) == {}

    def test_a_live_session_that_published_nothing_is_simply_absent(self, platform_root):
        assert statusline.by_session([{"id": SESSION, "live": True}]) == {}

    @pytest.mark.parametrize(
        "entry", ["junk", None, {}, {"live": True}, {"id": "", "live": True},
                  {"id": 7, "live": True}],
    )
    def test_an_entry_that_names_no_session_is_skipped(self, platform_root, entry):
        assert statusline.by_session([entry]) == {}

    def test_one_unreadable_document_does_not_cost_the_others(self, platform_root):
        """The bargain every read on the fleet path makes: this value is on the
        payload the whole app polls."""
        other = "s-20260903-141600-beef"
        _publish(raw="{torn")
        _publish(session_id=other)
        statuses = statusline.by_session(
            [{"id": SESSION, "live": True}, {"id": other, "live": True}], now=_now(),
        )
        assert list(statuses) == [other]


class TestContainerContract:
    def test_the_mount_destination_is_below_the_container_home(self):
        """A spawn refuses a drop-in's declared directory that is not strictly
        below the container home; the platform's own must satisfy the same
        rule."""
        assert statusline.CONTAINER_STATUS_DIR.startswith("/home/developer/")
        assert os.path.dirname(statusline.CONTAINER_STATUS_DIR) == "/home/developer"

    def test_the_document_name_matches_the_writer(self):
        """The two halves cannot share an import — the renderer is stdlib-only
        by constraint — so the name is pinned from both sides."""
        from hooks.statusline import STATUS_DIR_ENV, STATUS_FILENAME, STATUS_SCHEMA

        assert STATUS_FILENAME == statusline.STATUS_FILENAME
        assert STATUS_DIR_ENV == statusline.STATUS_DIR_ENV
        assert STATUS_SCHEMA == statusline.SCHEMA_VERSION
