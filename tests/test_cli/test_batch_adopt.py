from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hyperresearch.cli import app
from hyperresearch.core.adopt import adopt_path
from hyperresearch.core.note import write_note
from hyperresearch.core.sync import compute_sync_plan, execute_sync

runner = CliRunner()


def _seed_mixed_batch(tmp_vault, tmp_path):
    native = write_note(
        tmp_vault.notes_dir,
        "AAA Native",
        body="# Native\n\nMutable native note.\n",
        tags=["anc"],
        status="draft",
    )
    execute_sync(tmp_vault, compute_sync_plan(tmp_vault, force=True))

    source = tmp_path / "canonical-anc"
    source.mkdir()
    adopted = source / "ZZZ Adopted.md"
    adopted.write_text("# ZZZ Adopted\n\nCanonical project knowledge.\n", encoding="utf-8")
    adopt_path(tmp_vault, source, tag="anc")

    return native, adopted


def _json(result):
    return json.loads(result.stdout)


def test_adopted_batch_dry_run_fails_closed(tmp_vault, tmp_path, monkeypatch):
    native, adopted = _seed_mixed_batch(tmp_vault, tmp_path)
    monkeypatch.chdir(tmp_vault.root)
    native_before = native.read_bytes()
    adopted_before = adopted.read_bytes()

    result = runner.invoke(
        app,
        ["batch", "set-status", "evergreen", "--tag", "anc", "--dry-run", "--json"],
    )

    assert result.exit_code == 1
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error_code"] == "ADOPTED_READ_ONLY"
    assert "would_modify" not in result.stdout
    assert native.read_bytes() == native_before
    assert adopted.read_bytes() == adopted_before


def test_mixed_batch_rejects_before_native_mutation(tmp_vault, tmp_path, monkeypatch):
    native, adopted = _seed_mixed_batch(tmp_vault, tmp_path)
    monkeypatch.chdir(tmp_vault.root)
    native_before = native.read_bytes()
    adopted_before = adopted.read_bytes()

    result = runner.invoke(
        app,
        ["batch", "set-status", "evergreen", "--tag", "anc", "--json"],
    )

    assert result.exit_code == 1
    assert _json(result)["error_code"] == "ADOPTED_READ_ONLY"
    assert native.read_bytes() == native_before
    assert adopted.read_bytes() == adopted_before

    row = tmp_vault.db.execute(
        "SELECT status FROM notes WHERE title = 'AAA Native'"
    ).fetchone()
    assert row["status"] == "draft"


@pytest.mark.parametrize(
    "args",
    [
        ["batch", "tag-add", "new-tag", "--tag", "anc", "--json"],
        ["batch", "tag-remove", "anc", "--json"],
        ["batch", "set-status", "evergreen", "--tag", "anc", "--json"],
        ["batch", "deprecate", "--tag", "anc", "--json"],
        ["batch", "set-parent", "new-parent", "--tag", "anc", "--json"],
    ],
)
def test_all_batch_mutators_reject_adopted_notes_before_write(
    tmp_vault, tmp_path, monkeypatch, args
):
    native, adopted = _seed_mixed_batch(tmp_vault, tmp_path)
    monkeypatch.chdir(tmp_vault.root)
    native_before = native.read_bytes()
    adopted_before = adopted.read_bytes()

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert _json(result)["error_code"] == "ADOPTED_READ_ONLY"
    assert native.read_bytes() == native_before
    assert adopted.read_bytes() == adopted_before
