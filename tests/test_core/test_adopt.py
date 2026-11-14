"""Tests for reference-based adopted project corpora."""

from __future__ import annotations

from hyperresearch.core.adopt import adopt_path, sync_adoption
from hyperresearch.core.sync import compute_sync_plan, execute_sync
from hyperresearch.search.fts import search_fts


def test_adopt_indexes_canonical_markdown_without_copying(tmp_vault):
    source = tmp_vault.root / "docs" / "anc"
    source.mkdir(parents=True)
    canonical = source / "decision.md"
    canonical.write_text(
        "# ANC decision\n\nUnique adopted evidence phrase alpha-bridge.\n",
        encoding="utf-8",
    )

    result = adopt_path(tmp_vault, source, tag="anc")

    assert result.added == 1
    assert result.updated == 0
    assert canonical.exists()
    assert list(tmp_vault.notes_dir.rglob("*decision*.md")) == []

    row = tmp_vault.db.execute(
        "SELECT id, path, origin, canonical_path, adopt_group, adopt_kind "
        "FROM notes WHERE origin='adopted'"
    ).fetchone()
    assert row is not None
    assert row["origin"] == "adopted"
    assert row["canonical_path"] == "docs/anc/decision.md"
    assert row["adopt_group"] == "anc"
    assert row["path"].startswith("adopted/anc/")

    hits = search_fts(tmp_vault.db, "alpha bridge")
    assert [hit["id"] for hit in hits] == [row["id"]]
    assert hits[0]["origin"] == "adopted"
    assert hits[0]["canonical_path"] == "docs/anc/decision.md"

    # Native research/ sync sees no missing adopted pseudo-file.
    plan = compute_sync_plan(tmp_vault, force=True)
    assert row["path"] not in plan.to_delete
    execute_sync(tmp_vault, plan)
    still_there = tmp_vault.db.execute(
        "SELECT id FROM notes WHERE id = ?", (row["id"],)
    ).fetchone()
    assert still_there is not None


def test_adopt_sync_refreshes_same_identity_when_canonical_file_changes(tmp_vault):
    source = tmp_vault.root / "docs" / "anc"
    source.mkdir(parents=True)
    canonical = source / "verification.md"
    canonical.write_text(
        "# Verification receipt\n\nInitial searchable phrase.\n",
        encoding="utf-8",
    )

    first = adopt_path(tmp_vault, source, tag="anc")
    note_id = first.items[0].note_id
    first_hash = first.items[0].content_hash

    canonical.write_text(
        "# Verification receipt\n\nReplacement searchable phrase omega-close.\n",
        encoding="utf-8",
    )

    second = sync_adoption(tmp_vault, "anc")

    assert second.added == 0
    assert second.updated == 1
    assert second.items[0].note_id == note_id
    assert second.items[0].content_hash != first_hash

    row = tmp_vault.db.execute(
        "SELECT id, content_hash, origin FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["id"] == note_id
    assert row["origin"] == "adopted"
    assert row["content_hash"] == second.items[0].content_hash

    hits = search_fts(tmp_vault.db, "omega close")
    assert [hit["id"] for hit in hits] == [note_id]


def test_adopt_normalizes_generated_id_before_db_updates(tmp_vault):
    source = tmp_vault.root / "docs" / "anc"
    source.mkdir(parents=True)
    canonical = source / "evidence.md"
    canonical.write_text(
        "# Maternal Cardiac Disease Primary Source Notes — Very Long Title That Truncates Near A Separator\n\nEvidence.\n",
        encoding="utf-8",
    )

    result = adopt_path(tmp_vault, source, tag="anc")
    assert result.added == 1
    item = result.items[0]
    assert "--" not in item.note_id

    row = tmp_vault.db.execute(
        "SELECT id, origin, canonical_path FROM notes WHERE id = ?", (item.note_id,)
    ).fetchone()
    assert row is not None
    assert row["origin"] == "adopted"
    assert row["canonical_path"] == "docs/anc/evidence.md"

    follow_up = sync_adoption(tmp_vault, "anc", dry_run=True)
    assert follow_up.added == 0
    assert follow_up.unchanged == 1


def test_adopt_rebind_preserves_identity_for_same_relative_corpus(tmp_vault):
    first_root = tmp_vault.root / "worktree-a" / "docs" / "anc"
    first_root.mkdir(parents=True)
    (first_root / "decision.md").write_text(
        "# Decision\n\nSame corpus in the first worktree.\n", encoding="utf-8"
    )
    first = adopt_path(tmp_vault, first_root, tag="anc")
    note_id = first.items[0].note_id

    second_root = tmp_vault.root / "worktree-b" / "docs" / "anc"
    second_root.mkdir(parents=True)
    (second_root / "decision.md").write_text(
        "# Decision\n\nSame corpus after worktree merge/rebind.\n", encoding="utf-8"
    )
    second = adopt_path(tmp_vault, second_root, tag="anc", rebind=True)

    assert second.items[0].note_id == note_id
    row = tmp_vault.db.execute(
        "SELECT id, canonical_path FROM notes WHERE adopt_group='anc'"
    ).fetchone()
    assert row["id"] == note_id
    assert row["canonical_path"] == "worktree-b/docs/anc/decision.md"
    root = tmp_vault.db.execute(
        "SELECT root_path FROM adoption_roots WHERE tag='anc'"
    ).fetchone()
    assert root["root_path"] == "worktree-b/docs/anc"


def test_adopt_sync_remembers_explicit_kind_mode(tmp_vault):
    source = tmp_vault.root / "docs" / "anc"
    source.mkdir(parents=True)
    canonical = source / "plain.md"
    canonical.write_text("# Plain\n\nText.\n", encoding="utf-8")

    adopt_path(tmp_vault, source, tag="anc", kind="verification")
    canonical.write_text("# Plain\n\nChanged.\n", encoding="utf-8")
    sync_adoption(tmp_vault, "anc")

    row = tmp_vault.db.execute(
        "SELECT adopt_kind FROM notes WHERE origin='adopted'"
    ).fetchone()
    assert row["adopt_kind"] == "verification"
    root = tmp_vault.db.execute(
        "SELECT kind_mode FROM adoption_roots WHERE tag='anc'"
    ).fetchone()
    assert root["kind_mode"] == "verification"
