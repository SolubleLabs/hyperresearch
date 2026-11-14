"""File-to-DB sync engine — the bridge between markdown files and SQLite."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hyperresearch.core.note import read_note
from hyperresearch.core.sync_db import (
    _NOTE_ID_MOVED_TABLES,
    _NOTE_ID_REBUILT_TABLES,
    _NOTE_ID_URL_TABLES,
    _delete_note_from_db,
    _rename_note_id,
    _resolve_links_incremental,
    _upsert_note_to_db,
)

__all__ = [
    "_NOTE_ID_MOVED_TABLES",
    "_NOTE_ID_REBUILT_TABLES",
    "_NOTE_ID_URL_TABLES",
    "SyncPlan",
    "SyncResult",
    "compute_sync_plan",
    "execute_sync",
]


@dataclass
class SyncPlan:
    to_add: list[Path] = field(default_factory=list)
    to_update: list[Path] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)  # relative paths
    unchanged: int = 0


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: list[dict] = field(default_factory=list)
    duration_ms: float = 0


class _TransactionLostError(Exception):
    """SQLite rolled the whole sync transaction back in the middle of a pass."""


class _UnreadableNewFileError(Exception):
    """A new file named after an id in play has no id sync can read, so
    whether it claims that id is unknown."""

    def __init__(self, rel_path: str, reason: str) -> None:
        super().__init__(f"{rel_path}: {reason}")
        self.rel_path = rel_path
        self.reason = reason


def _should_exclude(rel_path: str, exclude_parts: list[str]) -> bool:
    """Fast exclusion check — match on first path component."""
    first = rel_path.split("/", 1)[0]
    return first in exclude_parts


_FRONTMATTER_PROBE = re.compile(rb"^---[ \t]*\r?\n")


def _has_frontmatter(path: Path) -> bool:
    """Cheap content probe — true iff the file opens with a YAML frontmatter
    delimiter (matching parse_frontmatter's regex, with optional UTF-8 BOM).

    Real notes — including stub notes under research/temp/ — always carry
    frontmatter (write_note() in core/note.py emits it unconditionally).
    Files without it are agent scratch artifacts that should never enter the
    note index (see issue #25): interim-report body files written before
    `note new --body-file`, evidence-digest.md, draft-{a,b,c}.md, and similar.
    Ingesting them produces same-id collisions with the canonical notes
    derived from them.
    """
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return _FRONTMATTER_PROBE.match(head) is not None


def compute_sync_plan(vault, force: bool = False) -> SyncPlan:
    """Compare disk state against DB state. Returns a plan."""
    plan = SyncPlan()

    # Only scan inside the research directory (notes/, index/)
    # This avoids walking .git/, .venv/, src/, etc. entirely.
    #
    # Files at the research/ root (e.g. research/scaffold.md,
    # research/comparisons.md, research/synthesis.md) are STAGING files the
    # agent writes then registers as real notes via `note new --body-file`.
    # They must NOT be synced as notes themselves — otherwise every run
    # produces 4 orphan notes and the missing-title/missing-tags/missing-summary
    # lint rules spam warnings for every research session.
    kb_dir = vault.research_dir
    if not kb_dir.exists():
        return plan

    runs_dir = kb_dir / "runs"
    disk_files: dict[str, float] = {}
    for md_file in kb_dir.rglob("*.md"):
        # Skip staging files at the research/ root. Real notes live in
        # research/notes/** or research/index/**.
        if md_file.parent == kb_dir:
            continue
        # Skip per-run workspaces entirely (research/runs/<vault_tag>/**) —
        # run-scoped pipeline artifacts are never vault notes.
        if runs_dir in md_file.parents:
            continue
        # Skip scratch artifacts without YAML frontmatter.
        if not _has_frontmatter(md_file):
            continue
        rel = md_file.relative_to(vault.root).as_posix()
        disk_files[rel] = md_file.stat().st_mtime

    # Load DB state — only catch "table not found" on fresh vaults
    db_state: dict[str, tuple[float, str]] = {}
    try:
        # Adopted rows are canonical files outside the native research/ sync
        # tree. They are refreshed only by the adopt workflow and must never be
        # interpreted as missing native notes here.
        for row in vault.db.execute(
            "SELECT path, file_mtime, content_hash FROM notes "
            "WHERE COALESCE(origin, 'native') != 'adopted'"
        ):
            db_state[row["path"]] = (row["file_mtime"], row["content_hash"])
    except sqlite3.OperationalError:
        # Table doesn't exist yet (fresh vault before schema init)
        pass

    for rel_path, mtime in disk_files.items():
        full_path = vault.root / rel_path
        if rel_path not in db_state:
            plan.to_add.append(full_path)
        elif force or abs(mtime - db_state[rel_path][0]) > 0.001:
            # mtime differs — verify with content hash (raw bytes)
            current_hash = hashlib.sha256(full_path.read_bytes()).hexdigest()
            if force or current_hash != db_state[rel_path][1]:
                plan.to_update.append(full_path)
            else:
                plan.unchanged += 1
        else:
            plan.unchanged += 1

    for db_path in db_state:
        if db_path not in disk_files:
            plan.to_delete.append(db_path)

    return plan


def execute_sync(vault, plan: SyncPlan) -> SyncResult:
    """Execute the sync plan within a single transaction for atomicity."""
    result = SyncResult(unchanged=plan.unchanged)
    start = time.monotonic()
    now_iso = datetime.now(UTC).isoformat()

    conn = vault.db
    conn.execute("BEGIN IMMEDIATE")

    changed_ids: set[str] = set()
    assets_root = vault.root / "research" / "assets"

    # Defense-in-depth (#25): refuse to silently smash an existing row's path
    # field when a second file in the same pass derives the same id, or when
    # a new file claims an id that's already in the DB at a different path.
    # The collision would otherwise be order-dependent and lose data on the
    # next `note update`.
    #
    # The reverse map gives the id the row at a path carries, so a file whose
    # `id:` changed is renamed in place; inserted as a new id, it was refused
    # by UNIQUE(notes.path) on every run.
    deleted_paths = set(plan.to_delete)
    id_to_path: dict[str, str] = {}
    path_to_id: dict[str, str] = {}
    for row in conn.execute("SELECT id, path FROM notes"):
        if row["path"] in deleted_paths:
            continue
        id_to_path[row["id"]] = row["path"]
        path_to_id[row["path"]] = row["id"]
    # Every new file by stem: a stem can be new in more than one directory
    # (research/notes/ and research/temp/).
    new_files_by_stem: dict[str, list[Path]] = {}
    for p in plan.to_add:
        new_files_by_stem.setdefault(p.stem, []).append(p)
    renamed_from: set[str] = set()

    def _new_files_claiming(note_id: str) -> list:
        """The new files named after `note_id` whose id is `note_id`.

        Fetch writes <id>.md and records its source and assets under the
        stem, so a new file named after an id is that id's owner unless it
        declares another. One whose frontmatter is there but does not parse
        (a file caught mid-write) raises: its id is unknown.
        """
        claimants = []
        for candidate in new_files_by_stem.get(note_id, ()):
            rel = candidate.relative_to(vault.root).as_posix()
            try:
                claimant = read_note(candidate, vault.root)
            except Exception as exc:
                raise _UnreadableNewFileError(rel, str(exc)) from exc
            if claimant.frontmatter_broken:
                raise _UnreadableNewFileError(rel, "its frontmatter does not parse")
            if claimant.meta.id == note_id:
                claimants.append(claimant)
        return claimants

    def _upsert_with_collision_check(file_path: Path) -> str | None:
        note = read_note(file_path, vault.root)
        rel = file_path.relative_to(vault.root).as_posix()
        existing = id_to_path.get(note.meta.id)
        if existing is not None and existing != rel:
            result.errors.append({
                "path": rel,
                "error": (
                    f"id collision: '{note.meta.id}' already belongs to "
                    f"'{existing}'. Skipped to avoid silent overwrite."
                ),
            })
            return None
        old_id = path_to_id.get(rel)
        rename = old_id is not None and old_id != note.meta.id
        if rename and not note.id_declared:
            # A broken, half-written or id-less frontmatter is no reason to
            # move the note's rows and free its id for another file.
            result.errors.append({
                "path": rel,
                "error": (
                    f"the indexed id is '{old_id}' but the frontmatter declares no id "
                    "(missing, or the frontmatter does not parse). Not re-indexed: "
                    "fix the frontmatter and run sync again."
                ),
            })
            return None
        keep_urls: set[str] = set()
        keep_assets_dir: Path | None = None
        if rename:
            try:
                new_id_claimants = _new_files_claiming(note.meta.id)
                # Rows under the old id were recorded for the file with that
                # stem: this one, or a new file named after the old id.
                old_id_claimants = [] if file_path.stem == old_id else _new_files_claiming(old_id)
            except _UnreadableNewFileError as exc:
                # A guess could take that file's id or move its URL rows.
                result.errors.append({
                    "path": rel,
                    "error": (
                        f"the id changed from '{old_id}' to '{note.meta.id}', but new "
                        f"file '{exc.rel_path}' is named after one of them and its id "
                        f"cannot be read ({exc.reason}). Not re-indexed: fix that file "
                        "and run sync again."
                    ),
                })
                return None
            if new_id_claimants:
                # Fetch recorded that file's URL under its stem, so the id is that file's.
                result.errors.append({
                    "path": rel,
                    "error": (
                        f"id collision: '{note.meta.id}' is claimed by new file "
                        f"'{new_id_claimants[0].path}'. Skipped to avoid silent overwrite."
                    ),
                })
                return None
            # Their URLs, and their assets in research/assets/<old_id>/, stay.
            keep_urls = {c.meta.source for c in old_id_claimants if c.meta.source}
            if old_id_claimants:
                keep_assets_dir = assets_root / old_id
        # A file's rename and upsert succeed or fail together, and the maps
        # change only after both: a rename left behind by a failed upsert
        # would park the row under an id a later file could take.
        conn.execute("SAVEPOINT sync_note")
        try:
            if rename:
                _rename_note_id(conn, old_id, note.meta.id, keep_urls, keep_assets_dir)
            _upsert_note_to_db(conn, note, now_iso, file_mtime=file_path.stat().st_mtime)
        except Exception:
            # On some errors (SQLITE_FULL among them) SQLite has already
            # rolled back the whole transaction, savepoints included, and a
            # ROLLBACK TO or RELEASE would raise "no such savepoint" in place
            # of the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK TO sync_note")
                conn.execute("RELEASE sync_note")
            raise
        conn.execute("RELEASE sync_note")
        if rename:
            # Free the old id for a later file in this pass.
            id_to_path.pop(old_id, None)
            changed_ids.add(old_id)
            renamed_from.add(old_id)
        id_to_path[note.meta.id] = rel
        path_to_id[rel] = note.meta.id
        changed_ids.add(note.meta.id)
        return note.meta.id

    def _record_error(path: str, exc: Exception) -> None:
        result.errors.append({"path": path, "error": str(exc)})
        if not conn.in_transaction:
            # SQLite rolled back the whole transaction (SQLITE_FULL among
            # others), but the maps still free the ids of undone deletes and
            # renames: a later file would land on a restored row and take its
            # children. Stop the pass, and say so in the error.
            result.errors[-1]["error"] += (
                "; nothing from this sync pass was kept: fix that error and run sync again"
            )
            raise _TransactionLostError from exc

    try:
        # Process deletes
        for rel_path in plan.to_delete:
            try:
                row = conn.execute("SELECT id FROM notes WHERE path = ?", (rel_path,)).fetchone()
                if row:
                    _delete_note_from_db(conn, row["id"])
                    changed_ids.add(row["id"])
                    result.deleted += 1
            except Exception as e:
                _record_error(rel_path, e)

        # Updates before adds: a rename frees an id that a new file in the
        # same pass may own, so this converges in one run. It also means that
        # when an id edit and a new file claim the same id, the edit wins and
        # the new file is refused as a collision, unless the new file is named
        # after that id (see _new_files_claiming).
        for file_path in plan.to_update:
            try:
                if _upsert_with_collision_check(file_path) is not None:
                    result.updated += 1
            except Exception as e:
                _record_error(str(file_path), e)

        # Process adds
        for file_path in plan.to_add:
            try:
                if _upsert_with_collision_check(file_path) is not None:
                    result.added += 1
            except Exception as e:
                _record_error(str(file_path), e)

        # Rows a rename kept for a claimant that then failed its own upsert
        # would fail the deferred foreign-key check: orphan them as a delete
        # of that id would (sources to NULL, assets rows gone, files kept).
        for old_id in renamed_from - set(id_to_path):
            conn.execute("UPDATE sources SET note_id = NULL WHERE note_id = ?", (old_id,))
            conn.execute("DELETE FROM assets WHERE note_id = ?", (old_id,))

        # Resolve links — only for changed notes' outgoing + incoming
        _resolve_links_incremental(conn, changed_ids)

        # Record sync timestamp
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('last_sync', ?)",
            (now_iso,),
        )
        conn.commit()
    except _TransactionLostError:
        # Nothing was kept, so nothing is counted; also discard whatever a
        # statement after the rollback opened on this connection.
        conn.rollback()
        result.added = result.updated = result.deleted = 0
    except Exception:
        conn.rollback()
        raise

    result.duration_ms = (time.monotonic() - start) * 1000
    return result
