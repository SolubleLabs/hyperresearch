"""Reference-based adoption of existing project Markdown into the vault index.

Adoption never copies or rewrites canonical project files. It stores their text in
SQLite/FTS as a cache, keeps the real path + content hash, and refreshes that
cache explicitly through the adopt workflow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hyperresearch.core.note import strip_markdown
from hyperresearch.core.sync_db import (
    _delete_note_from_db,
    _resolve_links_incremental,
    _upsert_note_to_db,
)
from hyperresearch.core.vault import Vault
from hyperresearch.models.note import ContentType, Note, NoteMeta, NoteStatus, NoteType, slugify

_SKIP_DIRS = {
    ".git",
    ".hyperresearch",
    ".venv",
    "node_modules",
    "templates",
    "__pycache__",
}

ADOPT_KINDS = {
    "project-document",
    "evidence",
    "clinical-synthesis",
    "product-requirement",
    "architecture-decision",
    "implementation-result",
    "verification",
    "human-acceptance",
    "open-question",
    "superseded-conclusion",
    "handoff",
}


class AdoptError(Exception):
    """Adoption request cannot be completed safely."""


@dataclass
class AdoptItem:
    canonical_path: str
    note_id: str
    action: str
    kind: str
    content_hash: str


@dataclass
class AdoptResult:
    tag: str
    root_path: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    items: list[AdoptItem] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.added + self.updated + self.unchanged


def _canonical_display_path(vault: Vault, path: Path) -> str:
    """Portable project-relative path when possible; absolute otherwise."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(vault.root).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_stored_path(vault: Vault, stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else vault.root / path


def _candidate_files(source_path: Path, vault: Vault) -> list[Path]:
    if source_path.is_file():
        if source_path.suffix.lower() != ".md":
            raise AdoptError(f"Adopted file must be Markdown: {source_path}")
        return [source_path]

    if not source_path.is_dir():
        raise AdoptError(f"Adoption source not found: {source_path}")

    research_dir = vault.research_dir.resolve()
    files: list[Path] = []
    for candidate in sorted(source_path.rglob("*.md")):
        resolved = candidate.resolve()
        if research_dir == resolved or research_dir in resolved.parents:
            # Native Hyperresearch notes/runs are already managed by normal sync.
            continue
        try:
            rel = resolved.relative_to(source_path)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        files.append(resolved)
    return files


def _extract_title(text: str, path: Path) -> str:
    """Use simple frontmatter/H1 hints without requiring project frontmatter schema."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            match = re.search(r"(?mi)^title\s*:\s*[\"']?(.+?)[\"']?\s*$", frontmatter)
            if match:
                title = match.group(1).strip().strip("\"'")
                if title:
                    return title
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def _summary(text: str) -> str:
    plain = " ".join(strip_markdown(text).split())
    return plain[:280] if plain else ""


def classify_adopt_kind(relative_path: str, title: str) -> str:
    """Conservative path/title heuristic; semantic assimilation can refine it later."""
    probe = f"{relative_path} {title}".lower().replace("\\", "/")
    if "handoff" in probe:
        return "handoff"
    if "human" in probe and ("acceptance" in probe or "usability" in probe):
        return "human-acceptance"
    if "supersed" in probe:
        return "superseded-conclusion"
    if "open-question" in probe or "open_question" in probe or "hold" in probe:
        return "open-question"
    if (
        "/qa/" in f"/{probe}"
        or "verifier" in probe
        or "receipt" in probe
        or "acceptance_matrix" in probe
        or "acceptance-matrix" in probe
        or "gates.md" in probe
    ):
        return "verification"
    if "implementation" in probe or "worktree_status" in probe or "worktree-status" in probe:
        return "implementation-result"
    if "architecture" in probe or "design-decision" in probe or "design_decision" in probe:
        return "architecture-decision"
    if "requirement" in probe or "prd" in probe:
        return "product-requirement"
    if (
        "primary_source" in probe
        or "primary-source" in probe
        or "source_adjudication" in probe
        or "source-adjudication" in probe
        or "literature" in probe
        or "evidence" in probe
    ):
        return "evidence"
    if "clinical-model" in probe or "clinical_model" in probe or "clinical-synthesis" in probe:
        return "clinical-synthesis"
    return "project-document"


def _deterministic_id(tag: str, canonical_path: str, title: str) -> str:
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:10]
    stem = slugify(title)[:40].rstrip("-") or "document"
    # NoteMeta normalizes ids through slugify too. Normalize here first so every
    # later UPDATE/lookup uses the exact id that will be stored in SQLite.
    return slugify(f"adopt-{slugify(tag)[:24]}-{stem}-{digest}")


def _virtual_path(tag: str, source_root: Path, path: Path) -> str:
    rel = Path(path.name) if source_root.is_file() else path.relative_to(source_root)
    return f"adopted/{slugify(tag)}/{rel.as_posix()}"


def adopt_path(
    vault: Vault,
    source: Path,
    *,
    tag: str,
    kind: str = "auto",
    prune: bool = True,
    dry_run: bool = False,
    rebind: bool = False,
) -> AdoptResult:
    """Adopt or refresh one file/directory without copying canonical bytes."""
    tag = slugify(tag)
    if not tag:
        raise AdoptError("Adoption tag must contain at least one letter or digit.")
    if kind != "auto" and kind not in ADOPT_KINDS:
        valid = ", ".join(sorted(ADOPT_KINDS))
        raise AdoptError(f"Unknown adopt kind {kind!r}. Use auto or one of: {valid}")

    source_path = source.resolve()
    files = _candidate_files(source_path, vault)
    root_display = _canonical_display_path(vault, source_path)
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    conn = vault.db

    existing_root = conn.execute(
        "SELECT root_path, kind_mode FROM adoption_roots WHERE tag = ?", (tag,)
    ).fetchone()
    if existing_root and existing_root["root_path"] != root_display and not rebind:
        raise AdoptError(
            f"Adoption tag {tag!r} already points to {existing_root['root_path']}; "
            "pass --rebind only when intentionally moving the same corpus root."
        )

    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT id, path, canonical_path, content_hash, adopt_kind FROM notes "
            "WHERE origin = 'adopted' AND adopt_group = ?",
            (tag,),
        )
    ]
    existing_by_canonical = {
        row["canonical_path"]: row for row in rows if row["canonical_path"]
    }
    existing_by_virtual = {row["path"]: row for row in rows}

    result = AdoptResult(tag=tag, root_path=root_display)
    seen_ids: set[str] = set()
    prepared: list[tuple[Path, str, str, str, str, str, str]] = []

    for path in files:
        canonical = _canonical_display_path(vault, path)
        raw = path.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
        title = _extract_title(text, path)
        relative = (
            path.name
            if source_path.is_file()
            else path.relative_to(source_path).as_posix()
        )
        adopt_kind = classify_adopt_kind(relative, title) if kind == "auto" else kind
        virtual_path = _virtual_path(tag, source_path, path)
        existing = existing_by_canonical.get(canonical) or existing_by_virtual.get(
            virtual_path
        )
        note_id = existing["id"] if existing else _deterministic_id(tag, canonical, title)
        seen_ids.add(note_id)
        action = "added"
        if existing:
            action = (
                "unchanged"
                if existing["content_hash"] == content_hash
                and existing["adopt_kind"] == adopt_kind
                else "updated"
            )
        if action == "added":
            result.added += 1
        elif action == "updated":
            result.updated += 1
        else:
            result.unchanged += 1
        result.items.append(
            AdoptItem(
                canonical_path=canonical,
                note_id=note_id,
                action=action,
                kind=adopt_kind,
                content_hash=content_hash,
            )
        )
        prepared.append(
            (path, canonical, virtual_path, note_id, text, title, adopt_kind)
        )

    missing = [row for row in rows if row["id"] not in seen_ids]
    if prune:
        result.removed = len(missing)
        result.removed_ids = [row["id"] for row in missing]

    if dry_run:
        return result

    conn.execute("BEGIN IMMEDIATE")
    changed_ids: set[str] = set()
    try:
        conn.execute(
            """
            INSERT INTO adoption_roots (tag, root_path, kind_mode, created_at, synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                root_path=excluded.root_path,
                kind_mode=excluded.kind_mode,
                synced_at=excluded.synced_at
            """,
            (tag, root_display, kind, now_iso, now_iso),
        )

        for path, canonical, virtual_path, note_id, text, title, adopt_kind in prepared:
            stat = path.stat()
            updated = datetime.fromtimestamp(stat.st_mtime, UTC)
            tags = ["adopted", tag, f"adopt-kind:{adopt_kind}"]
            meta = NoteMeta(
                title=title,
                id=note_id,
                tags=tags,
                status=NoteStatus.REVIEW,
                type=NoteType.NOTE,
                content_type=ContentType.DOCS,
                source=canonical,
                summary=_summary(text),
                created=updated,
                updated=updated,
            )
            note = Note(
                meta=meta,
                body=text,
                path=virtual_path,
                content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                word_count=len(text.split()),
            )
            _upsert_note_to_db(conn, note, now_iso, file_mtime=stat.st_mtime)
            conn.execute(
                """
                UPDATE notes
                SET origin='adopted',
                    canonical_path=?,
                    adopt_group=?,
                    adopt_kind=?
                WHERE id=?
                """,
                (canonical, tag, adopt_kind, note_id),
            )
            changed_ids.add(note_id)

        if prune:
            for row in missing:
                _delete_note_from_db(conn, row["id"])
                changed_ids.add(row["id"])

        _resolve_links_incremental(conn, changed_ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return result


def sync_adoption(
    vault: Vault,
    tag: str,
    *,
    prune: bool = True,
    dry_run: bool = False,
) -> AdoptResult:
    """Refresh one registered adoption root by tag."""
    normalized = slugify(tag)
    row = vault.db.execute(
        "SELECT root_path, kind_mode FROM adoption_roots WHERE tag = ?", (normalized,)
    ).fetchone()
    if not row:
        raise AdoptError(f"No adopted corpus registered with tag {normalized!r}.")
    source = _resolve_stored_path(vault, row["root_path"])
    return adopt_path(
        vault,
        source,
        tag=normalized,
        kind=row["kind_mode"],
        prune=prune,
        dry_run=dry_run,
    )
