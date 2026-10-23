"""Explicit version cutover preserves historical bytes and unknown metadata."""

import pytest


@pytest.fixture
def schema_one(tmp_path):
    root = tmp_path / "plexus"
    root.mkdir()
    (root / "_lmer.yaml").write_text(
        "# retain this comment\nschema: 1\nname: example\n"
        "custom:\n  field: yes\nprojects:\n  widgets:\n"
        "    repo: git.example.test/acme/widgets\n"
    )
    record = root / "widgets" / "_runs" / "chat" / "20260917-120000-1-7"
    record.mkdir(parents=True)
    (record / "state.yaml").write_text("schema: 1\nstatus: complete\ncustom: preserved\n")
    return root, record


def test_preview_then_apply_changes_only_version_token(schema_one):
    from lmer_schema.migration import migrate

    root, record = schema_one
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    preview = migrate(root)
    assert preview["moves"] == []
    assert preview["blockers"] == []
    assert "-schema: 1" in preview["diff"] and "+schema: 2" in preview["diff"]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    applied = migrate(root, apply=True)
    assert applied["applied"] is True
    assert (root / "_lmer.yaml").read_bytes() == before[next(p for p in before if p.name == "_lmer.yaml")].replace(b"schema: 1", b"schema: 2")
    assert (record / "state.yaml").read_bytes() == before[(record / "state.yaml").relative_to(root)]
    assert migrate(root, apply=True)["applied"] is False


def test_plain_session_records_block_migrate_apply(schema_one):
    from lmer_schema.migration import migrate
    from tests import plexus_fixtures as pf

    root, _record = schema_one
    pf.plant_plain_session_records(root / "widgets")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = migrate(root, apply=True)
    assert result["applied"] is False
    assert len(result["moves"]) == len(pf.PLAIN_SESSION_GROUPS)
    fold_blockers = [item for item in result["blockers"] if "work migrate" in item]
    assert len(fold_blockers) == len(pf.PLAIN_SESSION_GROUPS)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_a_group_with_two_sessions_is_one_blocker_and_one_move(schema_one):
    from lmer_schema.migration import migrate
    from tests import plexus_fixtures as pf

    root, _record = schema_one
    pf.plant_plain_session_records(root / "widgets")
    pf.plant_plain_session_records(root / "widgets", session_id="20260902-100000-1-9")
    result = migrate(root)
    assert len(result["moves"]) == len(pf.PLAIN_SESSION_GROUPS)
    assert {m["from"] for m in result["moves"]} == {
        f"widgets/_runs/{slug}" for _taskdef, _target, slug in pf.PLAIN_SESSION_GROUPS
    }
    assert all(m["to"] == f"widgets/_runs/{m['slug'].split()[-1]}" for m in result["moves"])
    fold_blockers = [item for item in result["blockers"] if "work migrate" in item]
    assert len(fold_blockers) == len(pf.PLAIN_SESSION_GROUPS)


def test_a_legacy_record_move_names_its_on_disk_dir(schema_one):
    from lmer_schema.migration import migrate
    from tests import plexus_fixtures as pf

    root, _record = schema_one
    pf.plant_plain_session_records(root / "widgets", area="runs", nested=False)
    named = root / "widgets" / "runs" / "chat-.--myname"
    (root / "widgets" / "runs" / "chat-.").rename(named)
    result = migrate(root)
    move = next(m for m in result["moves"] if m["slug"].startswith("chat-. "))
    assert move == {
        "from": "widgets/runs/chat-.--myname",
        "to": "widgets/_runs/chat",
        "slug": "chat-. \u2192 chat",
    }
    assert any(
        "`runs/chat-.--myname/ \u2192 _runs/chat/<session-id>/`" in item
        for item in result["blockers"]
    )


def test_a_host_bucket_never_blocks_the_cutover(schema_one):
    from lmer_schema.migration import migrate
    from tests import plexus_fixtures as pf

    root, _record = schema_one
    pf.plant_plain_session_records(root / "git.example.test" / "acme" / "gadgets")
    result = migrate(root)
    assert result["moves"] == []
    assert not any("work migrate" in item for item in result["blockers"])


def test_the_release_record_is_never_a_plain_session_blocker(schema_one):
    from lmer_schema.migration import migrate
    from tests import plexus_fixtures as pf

    root, _record = schema_one
    pf.plant_plain_session_records(root / "widgets")
    result = migrate(root)
    fold_blockers = [item for item in result["blockers"] if "work migrate" in item]
    for release in pf.RELEASE_RECORDS:
        slug = release["slug"]
        assert not any(move["from"].endswith(f"/{slug}") for move in result["moves"])
        assert not any(slug in blocker for blocker in fold_blockers)


def test_unsigned_active_record_blocks_cutover_without_mutation(schema_one):
    from lmer_schema.migration import migrate

    root, record = schema_one
    (record / "state.yaml").write_text("schema: 1\nstatus: in-progress\n")
    before = (root / "_lmer.yaml").read_bytes()
    result = migrate(root, apply=True)
    assert result["applied"] is False
    assert any("sign-out" in item for item in result["blockers"])
    assert (root / "_lmer.yaml").read_bytes() == before


def test_portless_real_bucket_blocks_cutover_but_compatibility_link_does_not(schema_one):
    from lmer_schema.migration import migrate

    root, _record = schema_one
    manifest = root / "_lmer.yaml"
    manifest.write_text(manifest.read_text().replace("git.example.test/acme", "git.example.test:8443/acme"))
    old = root / "git.example.test" / "acme" / "widgets"
    marker = old / "_runs"
    marker.mkdir(parents=True)
    before = manifest.read_bytes()
    result = migrate(root, apply=True)
    assert not result["applied"]
    assert any("port" in item and "git.example.test/acme/widgets" in item for item in result["blockers"])
    assert manifest.read_bytes() == before
    marker.rmdir()
    old.rmdir()
    old.symlink_to(root / "widgets", target_is_directory=True)
    assert migrate(root)["blockers"] == []


def test_malformed_record_error_does_not_echo_document_values(schema_one):
    from lmer_schema.migration import migrate

    root, record = schema_one
    (record / "state.yaml").write_text("schema: 1\napi_token: [do-not-print-this-secret\n")
    result = migrate(root)
    assert result["blockers"]
    assert "do-not-print-this-secret" not in str(result)


@pytest.mark.parametrize("declaration", [
    "schema: &version 1\ncustom: *version\n",
    "custom: &version 1\nschema: *version\n",
])
def test_anchored_schema_requires_explicit_review_without_rewriting_aliases(schema_one, declaration):
    from lmer_schema.migration import migrate

    root, _record = schema_one
    manifest = root / "_lmer.yaml"
    manifest.write_text(declaration + "name: example\n")
    before = manifest.read_bytes()
    result = migrate(root, apply=True)
    assert not result["applied"]
    assert any("anchor" in blocker for blocker in result["blockers"])
    assert manifest.read_bytes() == before
