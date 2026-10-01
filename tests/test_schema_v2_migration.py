"""Explicit version cutover preserves historical bytes and unknown metadata."""

import pytest


@pytest.fixture
def schema_one(tmp_path):
    root = tmp_path / "plexus"
    root.mkdir()
    (root / "_plexus.yaml").write_text(
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
    assert (root / "_plexus.yaml").read_bytes() == before[next(p for p in before if p.name == "_plexus.yaml")].replace(b"schema: 1", b"schema: 2")
    assert (record / "state.yaml").read_bytes() == before[(record / "state.yaml").relative_to(root)]
    assert migrate(root, apply=True)["applied"] is False


def test_unsigned_active_record_blocks_cutover_without_mutation(schema_one):
    from lmer_schema.migration import migrate

    root, record = schema_one
    (record / "state.yaml").write_text("schema: 1\nstatus: in-progress\n")
    before = (root / "_plexus.yaml").read_bytes()
    result = migrate(root, apply=True)
    assert result["applied"] is False
    assert any("sign-out" in item for item in result["blockers"])
    assert (root / "_plexus.yaml").read_bytes() == before


def test_portless_real_bucket_blocks_cutover_but_compatibility_link_does_not(schema_one):
    from lmer_schema.migration import migrate

    root, _record = schema_one
    manifest = root / "_plexus.yaml"
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
    manifest = root / "_plexus.yaml"
    manifest.write_text(declaration + "name: example\n")
    before = manifest.read_bytes()
    result = migrate(root, apply=True)
    assert not result["applied"]
    assert any("anchor" in blocker for blocker in result["blockers"])
    assert manifest.read_bytes() == before
