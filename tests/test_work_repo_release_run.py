"""Tests for the release-run memory kernel (masterplan release-flow §3):
release.yaml round-trip record/read, the derive_leg()/next_step() ladder
including the re-entered-leg-2 case, and both mismatch hard stops (version
at the release-MR merge SHA vs leg 1's record; tag SHA vs merge SHA)."""
from pathlib import Path

import pytest

from work_repo import release_run, run_state
from tests.conftest import strip_lmer_env

REPO_ROOT = Path(__file__).parent.parent

SHA_BUMP = "a" * 40
SHA_MERGE = "b" * 40
SHA_OTHER = "c" * 40


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _release_events(rdir):
    return [e for e in run_state.read_events(rdir, last_n=0) if e["type"] == "release"]


def _through_leg1(rdir):
    release_run.record_version(rdir, "0.5.0")
    return release_run.record_bump_merge(rdir, SHA_BUMP)


def _through_merge(rdir):
    _through_leg1(rdir)
    return release_run.record_release_merge(rdir, SHA_MERGE, version_at_sha="0.5.0")


def _through_tag(rdir):
    _through_merge(rdir)
    return release_run.record_tag(rdir, "v0.5.0", SHA_MERGE)


class TestLoadRelease:
    def test_absent_returns_none(self, tmp_path):
        assert release_run.load_release(tmp_path) is None

    def test_reads_written_release(self, tmp_path):
        release = release_run.seed_release()
        release["version"] = "0.5.0"
        release_run.write_release(tmp_path, release)
        loaded = release_run.load_release(tmp_path)
        assert loaded["version"] == "0.5.0"
        assert loaded["schema"] == release_run.RELEASE_SCHEMA_VERSION

    def test_missing_receipts_normalized_to_empty(self, tmp_path):
        (tmp_path / "release.yaml").write_text("schema: 1\n", encoding="utf-8")
        assert release_run.load_release(tmp_path)["receipts"] == {}

    def test_corrupt_yaml_backed_up(self, tmp_path):
        (tmp_path / "release.yaml").write_text("version: [unclosed", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="backed up"):
            release_run.load_release(tmp_path)
        assert not (tmp_path / "release.yaml").exists()
        assert list(tmp_path.glob("release.yaml.bad-*"))

    def test_non_mapping_backed_up(self, tmp_path):
        (tmp_path / "release.yaml").write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="not a mapping"):
            release_run.load_release(tmp_path)
        assert list(tmp_path.glob("release.yaml.bad-*"))

    def test_non_mapping_receipts_backed_up(self, tmp_path):
        (tmp_path / "release.yaml").write_text(
            "schema: 1\nreceipts: [a]\n", encoding="utf-8"
        )
        with pytest.raises(run_state.RunStateError, match="receipts field"):
            release_run.load_release(tmp_path)

    def test_non_mapping_tag_backed_up(self, tmp_path):
        (tmp_path / "release.yaml").write_text(
            "schema: 1\ntag: v0.5.0\n", encoding="utf-8"
        )
        with pytest.raises(run_state.RunStateError, match="tag field"):
            release_run.load_release(tmp_path)

    def test_newer_schema_read_only_refusal_leaves_file(self, tmp_path):
        (tmp_path / "release.yaml").write_text("schema: 99\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="read-only refusal"):
            release_run.load_release(tmp_path)
        assert (tmp_path / "release.yaml").exists()

    def test_bad_schema_type_backed_up(self, tmp_path):
        (tmp_path / "release.yaml").write_text("schema: true\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="schema field"):
            release_run.load_release(tmp_path)


class TestWriteRelease:
    def test_atomic_no_tmp_left(self, tmp_path):
        release_run.write_release(tmp_path, release_run.seed_release())
        assert (tmp_path / "release.yaml").exists()
        assert not (tmp_path / ".release.yaml.tmp").exists()

    def test_stamps_updated(self, tmp_path):
        release_run.write_release(tmp_path, release_run.seed_release())
        assert release_run.load_release(tmp_path)["updated"]


class TestRecordVersion:
    def test_creates_release_and_appends_event(self, tmp_path):
        release = release_run.record_version(tmp_path, "0.5.0")
        assert release["version"] == "0.5.0"
        assert release_run.load_release(tmp_path)["version"] == "0.5.0"
        events = _release_events(tmp_path)
        assert len(events) == 1
        assert events[0]["data"] == {"field": "version", "version": "0.5.0"}

    def test_rerecord_same_is_noop(self, tmp_path):
        release_run.record_version(tmp_path, "0.5.0")
        release_run.record_version(tmp_path, "0.5.0")
        assert len(_release_events(tmp_path)) == 1

    def test_rerecord_different_hard_stops(self, tmp_path):
        release_run.record_version(tmp_path, "0.5.0")
        with pytest.raises(release_run.ReleaseRunError, match="already recorded"):
            release_run.record_version(tmp_path, "0.6.0")
        assert release_run.load_release(tmp_path)["version"] == "0.5.0"

    def test_rejects_empty_and_non_string(self, tmp_path):
        for bad in ("", "  ", None, "0. 5"):
            with pytest.raises(release_run.ReleaseRunError, match="version"):
                release_run.record_version(tmp_path, bad)

    def test_rejects_tag_prefixed_version(self, tmp_path):
        # The record holds the pyproject version; the TAG adds the 'v'.
        with pytest.raises(release_run.ReleaseRunError, match="tag prefix"):
            release_run.record_version(tmp_path, "v0.5.0")


class TestRecordBumpMerge:
    def test_requires_version_first(self, tmp_path):
        with pytest.raises(release_run.ReleaseRunError, match="version"):
            release_run.record_bump_merge(tmp_path, SHA_BUMP)

    def test_records_and_appends_event(self, tmp_path):
        release = _through_leg1(tmp_path)
        assert release["bump_mr_merge_sha"] == SHA_BUMP
        assert _release_events(tmp_path)[-1]["data"] == {
            "field": "bump_mr_merge_sha", "sha": SHA_BUMP,
        }

    def test_rejects_short_sha(self, tmp_path):
        release_run.record_version(tmp_path, "0.5.0")
        with pytest.raises(release_run.ReleaseRunError, match="40-hex"):
            release_run.record_bump_merge(tmp_path, "abc1234")

    def test_normalizes_case(self, tmp_path):
        release_run.record_version(tmp_path, "0.5.0")
        release = release_run.record_bump_merge(tmp_path, "A" * 40)
        assert release["bump_mr_merge_sha"] == "a" * 40

    def test_rerecord_same_is_noop_different_hard_stops(self, tmp_path):
        _through_leg1(tmp_path)
        release_run.record_bump_merge(tmp_path, SHA_BUMP)
        assert len(_release_events(tmp_path)) == 2
        with pytest.raises(release_run.ReleaseRunError, match="already recorded"):
            release_run.record_bump_merge(tmp_path, SHA_OTHER)


class TestRecordReleaseMerge:
    def test_requires_leg1_records(self, tmp_path):
        with pytest.raises(release_run.ReleaseRunError, match="record leg 1"):
            release_run.record_release_merge(tmp_path, SHA_MERGE, "0.5.0")
        release_run.record_version(tmp_path, "0.5.0")
        with pytest.raises(release_run.ReleaseRunError, match="bump-MR merge SHA"):
            release_run.record_release_merge(tmp_path, SHA_MERGE, "0.5.0")

    def test_records_when_versions_agree(self, tmp_path):
        release = _through_merge(tmp_path)
        assert release["release_mr_merge_sha"] == SHA_MERGE

    def test_version_mismatch_hard_stops_and_writes_nothing(self, tmp_path):
        _through_leg1(tmp_path)
        with pytest.raises(release_run.ReleaseRunError,
                           match="HARD STOP.*second bump or foreign commit"):
            release_run.record_release_merge(tmp_path, SHA_MERGE, "0.6.0")
        assert release_run.load_release(tmp_path)["release_mr_merge_sha"] is None
        assert len(_release_events(tmp_path)) == 2  # only the leg-1 events

    def test_rerecord_same_is_noop_but_still_checks_version(self, tmp_path):
        _through_merge(tmp_path)
        release_run.record_release_merge(tmp_path, SHA_MERGE, "0.5.0")
        assert len(_release_events(tmp_path)) == 3
        # A re-entered leg 2 must re-prove the binding, not coast on it.
        with pytest.raises(release_run.ReleaseRunError, match="HARD STOP"):
            release_run.record_release_merge(tmp_path, SHA_MERGE, "0.6.0")

    def test_rerecord_different_sha_hard_stops(self, tmp_path):
        _through_merge(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="already recorded"):
            release_run.record_release_merge(tmp_path, SHA_OTHER, "0.5.0")


class TestRecordTag:
    def test_requires_merge_sha(self, tmp_path):
        _through_leg1(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="release-MR merge SHA"):
            release_run.record_tag(tmp_path, "v0.5.0", SHA_MERGE)

    def test_records_name_sha_created(self, tmp_path):
        release = _through_tag(tmp_path)
        tag = release["tag"]
        assert tag["name"] == "v0.5.0"
        assert tag["sha"] == SHA_MERGE
        assert tag["created"]
        assert _release_events(tmp_path)[-1]["data"] == {
            "field": "tag", "tag": "v0.5.0", "sha": SHA_MERGE,
        }

    def test_sha_disagreeing_with_merge_sha_hard_stops(self, tmp_path):
        _through_merge(tmp_path)
        with pytest.raises(release_run.ReleaseRunError,
                           match="HARD STOP.*never re-point, never re-sign"):
            release_run.record_tag(tmp_path, "v0.5.0", SHA_OTHER)
        assert release_run.load_release(tmp_path)["tag"] is None

    def test_unprefixed_tag_name_hard_stops(self, tmp_path):
        # The spec §1 damage: tag `0.2.0` pushed without the `v` prefix
        # never published — refused at record time.
        _through_merge(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="HARD STOP.*tag name"):
            release_run.record_tag(tmp_path, "0.5.0", SHA_MERGE)

    def test_wrong_version_tag_name_hard_stops(self, tmp_path):
        _through_merge(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="HARD STOP.*tag name"):
            release_run.record_tag(tmp_path, "v0.6.0", SHA_MERGE)

    def test_rerecord_identical_is_noop(self, tmp_path):
        _through_tag(tmp_path)
        events_before = len(_release_events(tmp_path))
        release_run.record_tag(tmp_path, "v0.5.0", SHA_MERGE)
        assert len(_release_events(tmp_path)) == events_before


class TestRecordReceipt:
    def test_unknown_receipt_rejected(self, tmp_path):
        _through_tag(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="unknown receipt"):
            release_run.record_receipt(tmp_path, "github-release")

    def test_requires_tag_recorded(self, tmp_path):
        _through_merge(tmp_path)
        with pytest.raises(release_run.ReleaseRunError, match="no tag recorded"):
            release_run.record_receipt(tmp_path, "github-main-push")

    def test_url_required_for_actions_run_and_pypi(self, tmp_path):
        _through_tag(tmp_path)
        for name in ("actions-run", "pypi"):
            with pytest.raises(release_run.ReleaseRunError, match="requires --url"):
                release_run.record_receipt(tmp_path, name)

    def test_records_row_and_event(self, tmp_path):
        _through_tag(tmp_path)
        release = release_run.record_receipt(
            tmp_path, "actions-run",
            url="https://github.com/lmer2/lmer/actions/runs/1", note="green",
        )
        row = release["receipts"]["actions-run"]
        assert row["url"] == "https://github.com/lmer2/lmer/actions/runs/1"
        assert row["note"] == "green"
        assert row["recorded"]
        assert _release_events(tmp_path)[-1]["data"] == {
            "field": "receipt", "receipt": "actions-run",
            "url": "https://github.com/lmer2/lmer/actions/runs/1", "note": "green",
        }

    def test_rerecord_replaces_url(self, tmp_path):
        # The re-dispatch case (spec §7 re-run artifact drift): the receipt
        # must record which Actions run ACTUALLY uploaded, so a re-dispatched
        # run replaces the URL; both writes stay in the audit trail.
        _through_tag(tmp_path)
        release_run.record_receipt(tmp_path, "actions-run", url="https://x/runs/1")
        release_run.record_receipt(tmp_path, "actions-run", url="https://x/runs/2")
        release = release_run.load_release(tmp_path)
        assert release["receipts"]["actions-run"]["url"] == "https://x/runs/2"
        urls = [e["data"]["url"] for e in _release_events(tmp_path)
                if e["data"].get("receipt") == "actions-run"]
        assert urls == ["https://x/runs/1", "https://x/runs/2"]

    def test_url_and_note_are_redacted(self, tmp_path, monkeypatch):
        _through_tag(tmp_path)
        monkeypatch.setattr(release_run, "redact_secrets", lambda s: "<redacted>")
        release = release_run.record_receipt(
            tmp_path, "pypi", url="https://pypi.org/?token=x", note="secret note"
        )
        row = release["receipts"]["pypi"]
        assert row["url"] == "<redacted>"
        assert row["note"] == "<redacted>"


class TestDeriveLeg:
    def test_no_release_is_leg1_bump(self):
        derived = release_run.derive_leg(None)
        assert (derived["leg"], derived["next_step"]) == ("leg1", "leg1-bump")
        assert derived["bump_merged"] is False
        assert derived["pushed"] == {"github_main": False, "github_tag": False,
                                     "gitlab_tag": False}
        assert release_run.derive_leg({})["next_step"] == "leg1-bump"

    def test_full_ladder_every_transition(self, tmp_path):
        # Drive the real recorders and check the derived leg after each.
        expect = [("leg1", "leg1-record-bump-merge"),
                  ("gate", "gate-await-release-merge"),
                  ("leg2", "leg2-create-tag"),
                  ("leg2", "leg2-push-github-main"),
                  ("leg2", "leg2-push-github-tag"),
                  ("leg2", "leg2-poll-actions"),
                  ("leg2", "leg2-record-pypi"),
                  ("leg2", "leg2-push-gitlab-tag"),
                  ("leg2", "leg2-dep-refresh"),
                  ("complete", "complete")]
        steps = [
            lambda: release_run.record_version(tmp_path, "0.5.0"),
            lambda: release_run.record_bump_merge(tmp_path, SHA_BUMP),
            lambda: release_run.record_release_merge(tmp_path, SHA_MERGE, "0.5.0"),
            lambda: release_run.record_tag(tmp_path, "v0.5.0", SHA_MERGE),
            lambda: release_run.record_receipt(tmp_path, "github-main-push"),
            lambda: release_run.record_receipt(tmp_path, "github-tag-push"),
            lambda: release_run.record_receipt(tmp_path, "actions-run",
                                               url="https://x/runs/2"),
            lambda: release_run.record_receipt(tmp_path, "pypi",
                                               url="https://pypi.org/project/lmer/0.5.0/"),
            lambda: release_run.record_receipt(tmp_path, "gitlab-tag-push"),
            lambda: release_run.record_receipt(tmp_path, "dep-refresh"),
        ]
        for step, (leg, nxt) in zip(steps, expect):
            release = step()
            derived = release_run.derive_leg(release)
            assert (derived["leg"], derived["next_step"]) == (leg, nxt)
        assert derived["version"] == "0.5.0"
        assert derived["bump_merged"] and derived["release_merged"]
        assert derived["tag_created"] and derived["tag"] == "v0.5.0"
        assert derived["merge_sha"] == SHA_MERGE
        assert derived["pushed"] == {"github_main": True, "github_tag": True,
                                     "gitlab_tag": True}
        assert derived["actions_run_url"] == "https://x/runs/2"
        assert derived["pypi_url"] == "https://pypi.org/project/lmer/0.5.0/"

    def test_reentered_leg2_resumes_from_disk_alone(self, tmp_path):
        # Session died after the GitHub main push: a relaunch re-reads
        # release.yaml (no remotes) and lands mid-ladder, and the recorders
        # it replays on the way in are no-ops.
        _through_tag(tmp_path)
        release_run.record_receipt(tmp_path, "github-main-push")
        reloaded = release_run.load_release(tmp_path)  # the relaunch
        derived = release_run.derive_leg(reloaded)
        assert derived["leg"] == "leg2"
        assert derived["next_step"] == "leg2-push-github-tag"
        events_before = len(_release_events(tmp_path))
        release_run.record_release_merge(tmp_path, SHA_MERGE, "0.5.0")
        release_run.record_tag(tmp_path, "v0.5.0", SHA_MERGE)
        assert len(_release_events(tmp_path)) == events_before

    def test_out_of_order_receipts_converge_at_first_gap(self, tmp_path):
        # gitlab-tag-push recorded but no actions-run receipt: the ladder
        # stops at the gap — public-before-internal ordering is re-imposed.
        _through_tag(tmp_path)
        release_run.record_receipt(tmp_path, "github-main-push")
        release_run.record_receipt(tmp_path, "github-tag-push")
        release_run.record_receipt(tmp_path, "gitlab-tag-push")
        derived = release_run.derive_leg(release_run.load_release(tmp_path))
        assert derived["next_step"] == "leg2-poll-actions"
        assert derived["pushed"]["gitlab_tag"] is True

    def test_hand_edited_tag_sha_mismatch_hard_stops(self, tmp_path):
        release = _through_tag(tmp_path)
        release["tag"] = dict(release["tag"], sha=SHA_OTHER)
        with pytest.raises(release_run.ReleaseRunError, match="never re-point"):
            release_run.derive_leg(release)

    def test_hand_edited_tag_name_mismatch_hard_stops(self, tmp_path):
        release = _through_tag(tmp_path)
        release["tag"] = dict(release["tag"], name="v0.6.0")
        with pytest.raises(release_run.ReleaseRunError, match="does not match"):
            release_run.derive_leg(release)

    def test_tolerates_malformed_receipt_rows(self, tmp_path):
        release = _through_tag(tmp_path)
        release["receipts"] = {"github-main-push": "not-a-dict",
                               "actions-run": None}
        derived = release_run.derive_leg(release)
        # Presence still counts for the ladder; URLs degrade to None.
        assert derived["next_step"] == "leg2-push-github-tag"
        assert derived["actions_run_url"] is None


class TestNextStep:
    def test_delegates_to_derive(self, tmp_path):
        assert release_run.next_step(None) == "leg1-bump"
        _through_merge(tmp_path)
        assert release_run.next_step(release_run.load_release(tmp_path)) == "leg2-create-tag"


class TestFormatReleaseStatus:
    def test_no_release(self):
        assert release_run.format_release_status(None) == "No release recorded"

    def test_full_status(self, tmp_path):
        _through_tag(tmp_path)
        release_run.record_receipt(tmp_path, "github-main-push")
        text = release_run.format_release_status(release_run.load_release(tmp_path))
        assert "Release: 0.5.0 — leg2 (next: leg2-push-github-tag)" in text
        assert f"v0.5.0 @ {SHA_MERGE}" in text
        assert "github-main-push" in text and "✓" in text
        assert "gitlab-tag-push" in text and "—" in text


class TestReleaseSlug:
    """The version-bearing address a release run moves to — what makes the
    NEXT release of a repository a new run instead of a permanent refusal."""

    def test_version_bearing_slug(self):
        assert release_run.release_slug("release-global", "0.6.0") == \
            "release-global-v0.6.0"

    def test_the_record_still_refuses_a_v_prefixed_version(self):
        """The slug adds the `v`; a recorded `v0.6.0` would make it `vv0.6.0`."""
        with pytest.raises(release_run.ReleaseRunError, match="tag prefix"):
            release_run.release_slug("release-global", "v0.6.0")

    def test_no_version_falls_back_to_a_compact_utc_stamp(self):
        """An abandoned release aborted before leg 1 has no version to name
        it with — it still has to vacate the bare address."""
        slug = release_run.release_slug("release-global")

        assert slug.startswith("release-global-")
        stamp = slug[len("release-global-"):]
        assert stamp.endswith("Z") and "-" not in stamp and ":" not in stamp
        assert len(stamp) == len("20260728T031122Z")

    def test_the_stamp_form_is_never_mistaken_for_a_version(self):
        assert not release_run.release_slug("release-global").startswith(
            "release-global-v")


class TestUniqueReleaseSlug:
    """`release_slug` names the address a run WANTS; this is one it can
    actually take. A version repeats — a declined release's successor records
    the same X.Y.Z while the declined run is still parked on that address —
    so the canonical form is not always free."""

    def test_the_canonical_address_when_it_is_free(self, tmp_path):
        assert release_run.unique_release_slug(
            "release-global", "0.6.0", tmp_path) == "release-global-v0.6.0"

    def test_a_taken_canonical_address_yields_a_free_variant(self, tmp_path):
        (tmp_path / "release-global-v0.6.0").mkdir()
        slug = release_run.unique_release_slug(
            "release-global", "0.6.0", tmp_path)

        assert slug.startswith("release-global-v0.6.0-")  # version stays legible
        assert run_state.slug_available(slug, tmp_path)

    def test_exhaustion_raises_instead_of_returning_a_taken_address(
            self, tmp_path, monkeypatch):
        """The contract is total. Handing back an address `slug_available`
        has just refused would drop the caller straight back into the
        skipped-re-slug wedge this function exists to remove — so the
        exhausted search raises and the callers take their documented
        no-address-available path."""
        monkeypatch.setattr(release_run, "_UNIQUE_SLUG_ATTEMPTS", 4)
        monkeypatch.setattr(release_run, "utc_now_iso",
                            lambda: "2031-02-03T04:05:06Z")
        stamped = "release-global-v0.6.0-20310203T040506Z"
        for name in ("release-global-v0.6.0", stamped,
                     f"{stamped}-2", f"{stamped}-3"):
            (tmp_path / name).mkdir()

        with pytest.raises(release_run.ReleaseRunError,
                           match="no free release address"):
            release_run.unique_release_slug("release-global", "0.6.0", tmp_path)


class TestReceiptNameCopies:
    """Every prose copy of the receipt-name list names every RECEIPT_NAMES
    entry. The tuple is the kernel's, but a release session reads the CLI
    `--help`, the module docstring's frozen verb block and the `work` CLI
    reference — each presents its list as complete, so a name added to the
    tuple and nowhere else leaves a user five accepted names out of six.
    Source-level guard: it fails when the tuple grows, not when a release
    runs."""

    COPIES = (
        # `work release record receipt --help` — user-visible.
        "src/work_repo/cli.py",
        # The `work` CLI reference `work-view-docs` serves, whose
        # "Push/upload receipts, in ladder order" block reads as exhaustive.
        "lmer-docs/WORK-REPO.md",
        # The release.yaml schema reference.
        "docs/RUN-STATE.md",
    )

    @pytest.mark.parametrize("rel", COPIES)
    def test_copy_names_every_receipt(self, rel):
        text = (REPO_ROOT / rel).read_text()
        missing = [n for n in release_run.RECEIPT_NAMES if n not in text]
        assert not missing, f"{rel} omits {missing}"

    def test_frozen_verb_block_names_every_receipt(self):
        """The module docstring, checked apart from the file: the tuple
        literal lives in the same file, so a whole-file substring test
        would match itself and pass on a stale verb block."""
        doc = release_run.__doc__
        missing = [n for n in release_run.RECEIPT_NAMES if n not in doc]
        assert not missing, f"the frozen verb block omits {missing}"
