"""SQLite persistence helpers used by native sync and adopted corpora."""

from __future__ import annotations

from pathlib import Path

from hyperresearch.core.note import strip_markdown
from hyperresearch.core.patterns import WIKI_LINK_RE, is_valid_wiki_link_target, strip_code

# Child tables keyed by note_id. A rename drops the rows the upsert rebuilds
# from the markdown, and moves the rows only the DB holds, which a delete and
# re-insert would cascade away, null out (`sources`) or leave pointing at a
# dead id (`escalations` has no foreign key).
_NOTE_ID_REBUILT_TABLES = ("note_content", "tags", "aliases")
_NOTE_ID_MOVED_TABLES = ("embeddings", "claims", "assets")
_NOTE_ID_URL_TABLES = ("sources", "escalations")


def _rename_note_id(
    conn,
    old_id: str,
    new_id: str,
    keep_urls: set[str],
    keep_assets_dir: Path | None = None,
) -> None:
    """Move a note row to a new id in place, with the rows only the DB holds.

    No foreign key declares ON UPDATE, so the parent-key change and the
    child re-pointing cannot both pass an immediate check: the check is
    deferred to commit. The pragma lasts until the transaction ends, not
    the savepoint; no other statement in a sync pass can violate a foreign
    key (each inserts a child after its parent or deletes through the
    cascade), so that changes nothing for them. `sources` and
    `escalations` rows whose URL is in `keep_urls`, and `assets` rows whose
    file lies in `keep_assets_dir`, stay under the old id for the new file
    that owns it; the caller orphans them if no row holds that id at commit.
    Asset files never move: their rows name them by full path.

    Caller guarantees `new_id` is not in use.
    """
    from hyperresearch.core.claims import _under

    conn.execute("PRAGMA defer_foreign_keys = ON")
    for table in _NOTE_ID_REBUILT_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE note_id = ?", (old_id,))
    conn.execute("DELETE FROM links WHERE source_id = ?", (old_id,))
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (old_id,))
    conn.execute("UPDATE notes SET id = ? WHERE id = ?", (new_id, old_id))
    kept_assets: list[int] = []
    if keep_assets_dir is not None:
        kept_assets = [
            row["id"]
            for row in conn.execute("SELECT id, filename FROM assets WHERE note_id = ?", (old_id,))
            if _under(Path(row["filename"]), keep_assets_dir)
        ]
    for table in _NOTE_ID_MOVED_TABLES:
        not_kept = ""
        params: tuple = (new_id, old_id)
        if table == "assets" and kept_assets:
            not_kept = f" AND id NOT IN ({','.join('?' for _ in kept_assets)})"
            params += tuple(kept_assets)
        conn.execute(f"UPDATE {table} SET note_id = ? WHERE note_id = ?{not_kept}", params)
    keep = sorted(keep_urls)
    not_kept = f" AND url NOT IN ({','.join('?' for _ in keep)})" if keep else ""
    for table in _NOTE_ID_URL_TABLES:
        conn.execute(
            f"UPDATE {table} SET note_id = ? WHERE note_id = ?{not_kept}",
            (new_id, old_id, *keep),
        )


def _upsert_note_to_db(conn, note, synced_at: str, file_mtime: float = 0) -> None:
    """Insert or update a note and all related tables using proper UPSERT."""
    meta = note.meta
    created_iso = meta.created.isoformat() if meta.created else synced_at
    updated_iso = meta.updated.isoformat() if meta.updated else None

    reviewed_iso = meta.reviewed.isoformat() if meta.reviewed else None
    expires_iso = meta.expires.isoformat() if meta.expires else None

    # Use INSERT ... ON CONFLICT to avoid CASCADE deletes from INSERT OR REPLACE
    # NOTE: the derived score columns (authority_score, centrality_score,
    # independence, quality_score) are deliberately absent — they are DB-cache
    # values computed by `hpr sources score` / `hpr graph rank` and must
    # survive re-syncs. Frontmatter-mirrored ranking fields (doi,
    # utility_score, citation_count, venue, is_retracted) sync normally.
    conn.execute(
        """
        INSERT INTO notes
            (id, title, path, status, type, tier, content_type, source, parent,
             deprecated, reviewed, expires, word_count, summary,
             created, updated, file_mtime, content_hash, synced_at,
             doi, utility_score, citation_count, venue, is_retracted,
             oa_url, oa_source, oa_version, oa_license, oa_recovery_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, path=excluded.path, status=excluded.status,
            type=excluded.type, tier=excluded.tier, content_type=excluded.content_type,
            source=excluded.source, parent=excluded.parent,
            deprecated=excluded.deprecated,
            reviewed=excluded.reviewed, expires=excluded.expires,
            word_count=excluded.word_count, summary=excluded.summary,
            created=excluded.created, updated=excluded.updated,
            file_mtime=excluded.file_mtime, content_hash=excluded.content_hash,
            synced_at=excluded.synced_at,
            doi=excluded.doi, utility_score=excluded.utility_score,
            citation_count=excluded.citation_count, venue=excluded.venue,
            is_retracted=excluded.is_retracted,
            oa_url=excluded.oa_url, oa_source=excluded.oa_source,
            oa_version=excluded.oa_version, oa_license=excluded.oa_license,
            oa_recovery_kind=excluded.oa_recovery_kind
        """,
        (
            meta.id, meta.title, note.path, meta.status, meta.type,
            meta.tier, meta.content_type,
            meta.source, meta.parent, 1 if meta.deprecated else 0,
            reviewed_iso, expires_iso,
            note.word_count, meta.summary, created_iso, updated_iso,
            file_mtime, note.content_hash, synced_at,
            meta.doi, meta.utility_score, meta.citation_count, meta.venue,
            None if meta.is_retracted is None else int(meta.is_retracted),
            meta.oa_url, meta.oa_source, meta.oa_version, meta.oa_license,
            meta.oa_recovery_kind,
        ),
    )

    # Update tags — resolve aliases before writing
    conn.execute("DELETE FROM tags WHERE note_id = ?", (meta.id,))
    alias_map = {}
    try:
        for row in conn.execute("SELECT alias, canonical FROM tag_aliases"):
            alias_map[row["alias"]] = row["canonical"]
    except Exception:
        pass
    for tag in meta.tags:
        # Lowercase the frontmatter tag for BOTH the lookup and the fallback:
        # alias keys and canonicals are stored lowercase (cli/tag.py), and
        # SearchFilters lowercases query-side tags, so the stored value must
        # be lowercase to match. Without this, a frontmatter tag like `LLM`
        # misses its alias mapping and is invisible to `--tag llm` filtering.
        resolved = alias_map.get(tag.lower(), tag.lower())
        conn.execute("INSERT OR IGNORE INTO tags (note_id, tag) VALUES (?, ?)", (meta.id, resolved))

    # Update aliases
    conn.execute("DELETE FROM aliases WHERE note_id = ?", (meta.id,))
    for alias in meta.aliases:
        conn.execute("INSERT INTO aliases (note_id, alias) VALUES (?, ?)", (meta.id, alias))

    # Update content (UPSERT)
    body_plain = strip_markdown(note.body)
    conn.execute(
        """
        INSERT INTO note_content (note_id, body, body_plain) VALUES (?, ?, ?)
        ON CONFLICT(note_id) DO UPDATE SET body=excluded.body, body_plain=excluded.body_plain
        """,
        (meta.id, note.body, body_plain),
    )

    # Update FTS — delete then insert (FTS5 doesn't support ON CONFLICT)
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (meta.id,))
    conn.execute(
        "INSERT INTO notes_fts (id, title, body_plain, tags, aliases) VALUES (?, ?, ?, ?, ?)",
        (meta.id, meta.title, body_plain, " ".join(meta.tags), " ".join(meta.aliases)),
    )

    # Update links — extract from original body (not stripped)
    # Uses the shared filter so this path stays in sync with core.note.read_note
    conn.execute("DELETE FROM links WHERE source_id = ?", (meta.id,))
    cleaned = strip_code(note.body)
    for line_num, line in enumerate(cleaned.split("\n"), 1):
        for m in WIKI_LINK_RE.finditer(line):
            target_ref = m.group(1).strip().rstrip("\\")  # Strip trailing backslash (shell escaping artifact)
            if not is_valid_wiki_link_target(target_ref):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO links (source_id, target_ref, line_number, context) "
                "VALUES (?, ?, ?, ?)",
                (meta.id, target_ref, line_num, line.strip()[:200]),
            )


def _delete_note_from_db(conn, note_id: str) -> None:
    """Delete a note and all related data (CASCADE handles child tables)."""
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
    conn.execute("DELETE FROM links WHERE source_id = ?", (note_id,))
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def _resolve_links_incremental(conn, changed_ids: set[str]) -> None:
    """Resolve links incrementally — only for links affected by changed notes.

    This handles two cases:
    1. Outgoing links from changed notes need resolution
    2. Unresolved links from ANY note might now resolve to a newly added note
    """
    if not changed_ids:
        return

    # Re-resolve outgoing links from changed notes
    placeholders = ",".join("?" for _ in changed_ids)
    ids = list(changed_ids)

    # Reset target_id for links from changed notes
    conn.execute(
        f"UPDATE links SET target_id = NULL WHERE source_id IN ({placeholders})", ids
    )

    # Also reset links that pointed TO deleted/changed notes (they may have moved)
    conn.execute(
        f"UPDATE links SET target_id = NULL WHERE target_id IN ({placeholders})", ids
    )

    # Now resolve all currently-NULL links (which includes the ones we just reset
    # plus any that were already broken and might now resolve)
    _resolve_null_links(conn)


def _resolve_null_links(conn) -> None:
    """Resolve all links with target_id = NULL."""
    # Exact ID match
    conn.execute("""
        UPDATE links SET target_id = (
            SELECT n.id FROM notes n WHERE n.id = links.target_ref LIMIT 1
        )
        WHERE target_id IS NULL
    """)
    # Case-insensitive ID match
    conn.execute("""
        UPDATE links SET target_id = (
            SELECT n.id FROM notes n WHERE LOWER(n.id) = LOWER(links.target_ref) LIMIT 1
        )
        WHERE target_id IS NULL
    """)
    # Alias match
    conn.execute("""
        UPDATE links SET target_id = (
            SELECT a.note_id FROM aliases a
            WHERE LOWER(a.alias) = LOWER(links.target_ref)
            LIMIT 1
        )
        WHERE target_id IS NULL
    """)
    # Title match
    conn.execute("""
        UPDATE links SET target_id = (
            SELECT n.id FROM notes n WHERE LOWER(n.title) = LOWER(links.target_ref) LIMIT 1
        )
        WHERE target_id IS NULL
    """)
