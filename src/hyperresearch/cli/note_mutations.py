"""Mutating note commands kept separate from note read/create commands.

This split keeps the CRUD surface reviewable and gives adopted-corpus write guards
a single small module without changing the public ``hyperresearch note`` CLI.
"""

from __future__ import annotations

from datetime import UTC

import typer

from hyperresearch.cli._output import console, output
from hyperresearch.models.output import error, success


def note_edit(
    note_id: str = typer.Argument(..., help="Note ID to edit"),
) -> None:
    """Open a note in $EDITOR."""
    import os
    import subprocess

    from hyperresearch.core.vault import Vault

    vault = Vault.discover()
    vault.auto_sync()

    row = vault.db.execute(
        "SELECT path, origin FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if not row:
        console.print(f"[red]Not found:[/] {note_id}")
        raise typer.Exit(1)
    if row["origin"] == "adopted":
        console.print(
            "[red]Adopted notes are read-only.[/] Edit the canonical project file "
            "and run hyperresearch adopt-sync instead."
        )
        raise typer.Exit(1)

    file_path = vault.root / row["path"]
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "notepad" if os.name == "nt" else "vim"))
    subprocess.run([editor, str(file_path)])


def note_update(
    note_id: str = typer.Argument(..., help="Note ID"),
    set_status: str | None = typer.Option(None, "--status", "-s", help="Set status"),
    add_tag: list[str] = typer.Option([], "--add-tag", help="Add tag(s)"),
    remove_tag: list[str] = typer.Option([], "--remove-tag", help="Remove tag(s)"),
    set_summary: str | None = typer.Option(None, "--summary", help="Set summary"),
    set_parent: str | None = typer.Option(None, "--parent", "-p", help="Set parent topic"),
    set_source: str | None = typer.Option(None, "--source", help="Set source URL/path"),
    set_tier: str | None = typer.Option(None, "--tier", help="Set epistemic tier: ground_truth|institutional|practitioner|commentary|unknown"),
    set_content_type: str | None = typer.Option(None, "--content-type", help="Set artifact kind: paper|docs|article|blog|forum|dataset|policy|code|book|transcript|review|unknown"),
    deprecate: bool = typer.Option(False, "--deprecate", help="Mark as deprecated"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Update frontmatter fields on a single note."""
    from datetime import datetime

    from hyperresearch.core.frontmatter import parse_frontmatter, serialize_frontmatter
    from hyperresearch.core.sync import compute_sync_plan, execute_sync
    from hyperresearch.core.vault import Vault
    from hyperresearch.models.note import ContentType, NoteStatus, Tier

    # Validate enums up-front
    if set_status is not None:
        try:
            NoteStatus(set_status)
        except ValueError:
            valid = ", ".join(s.value for s in NoteStatus)
            if json_output:
                output(error(f"Invalid --status '{set_status}'. Must be one of: {valid}", "INVALID_STATUS"), json_mode=True)
            else:
                console.print(f"[red]Invalid --status '{set_status}'.[/] Must be one of: {valid}")
            raise typer.Exit(1)
    if set_tier is not None:
        try:
            Tier(set_tier)
        except ValueError:
            valid = ", ".join(t.value for t in Tier)
            if json_output:
                output(error(f"Invalid --tier '{set_tier}'. Must be one of: {valid}", "INVALID_TIER"), json_mode=True)
            else:
                console.print(f"[red]Invalid --tier '{set_tier}'.[/] Must be one of: {valid}")
            raise typer.Exit(1)
    if set_content_type is not None:
        try:
            ContentType(set_content_type)
        except ValueError:
            valid = ", ".join(c.value for c in ContentType)
            if json_output:
                output(error(f"Invalid --content-type '{set_content_type}'. Must be one of: {valid}", "INVALID_CONTENT_TYPE"), json_mode=True)
            else:
                console.print(f"[red]Invalid --content-type '{set_content_type}'.[/] Must be one of: {valid}")
            raise typer.Exit(1)

    vault = Vault.discover()
    vault.auto_sync()

    row = vault.db.execute(
        "SELECT path, origin FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if not row:
        if json_output:
            output(error(f"Note not found: {note_id}", "NOT_FOUND"), json_mode=True)
        else:
            console.print(f"[red]Not found:[/] {note_id}")
        raise typer.Exit(1)
    if row["origin"] == "adopted":
        msg = (
            "Adopted notes are read-only. Edit the canonical project file and "
            "run hyperresearch adopt-sync instead."
        )
        if json_output:
            output(error(msg, "ADOPTED_READ_ONLY"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)

    file_path = vault.root / row["path"]
    content = file_path.read_text(encoding="utf-8-sig")
    meta, body = parse_frontmatter(content)

    changed = []
    if set_status:
        meta.status = set_status
        changed.append(f"status={set_status}")
    for t in add_tag:
        if t.lower() not in meta.tags:
            meta.tags.append(t.lower())
            changed.append(f"+tag:{t}")
    for t in remove_tag:
        if t.lower() in meta.tags:
            meta.tags.remove(t.lower())
            changed.append(f"-tag:{t}")
    if set_summary is not None:
        meta.summary = set_summary
        changed.append("summary")
    if set_parent is not None:
        meta.parent = set_parent
        changed.append(f"parent={set_parent}")
    if set_source is not None:
        meta.source = set_source
        changed.append("source")
    if set_tier is not None:
        meta.tier = set_tier
        changed.append(f"tier={set_tier}")
    if set_content_type is not None:
        meta.content_type = set_content_type
        changed.append(f"content_type={set_content_type}")
    if deprecate:
        meta.deprecated = True
        meta.status = "deprecated"
        changed.append("deprecated")

    if not changed:
        if json_output:
            output(success({"id": note_id, "changed": []}, vault=str(vault.root)), json_mode=True)
        else:
            console.print("[dim]Nothing to update.[/]")
        return

    meta.updated = datetime.now(UTC)
    new_content = serialize_frontmatter(meta) + "\n" + body
    file_path.write_text(new_content, encoding="utf-8")

    plan = compute_sync_plan(vault)
    execute_sync(vault, plan)

    if json_output:
        output(success({"id": note_id, "changed": changed}, vault=str(vault.root)), json_mode=True)
    else:
        console.print(f"[green]Updated {note_id}:[/] {', '.join(changed)}")


def note_mv(
    note_id: str = typer.Argument(..., help="Note ID to move"),
    new_path: str = typer.Argument(
        ...,
        help=(
            "Destination relative to the vault root, e.g. research/notes/renamed.md. "
            "A bare name lands in research/notes/ and a missing .md is added. The note "
            "keeps its id (it lives in frontmatter, not the filename), so wiki-links to "
            "it stay valid and nothing else is rewritten."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Move a note file within the tree sync scans (research/notes or research/temp).

    The id is unchanged: `mv` moves the file, it does not rename the note.
    """
    from pathlib import Path

    from hyperresearch.core.claims import _under
    from hyperresearch.core.sync import compute_sync_plan, execute_sync
    from hyperresearch.core.vault import Vault

    vault = Vault.discover()
    vault.auto_sync()

    row = vault.db.execute(
        "SELECT path, origin FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if not row:
        if json_output:
            output(error(f"Note not found: {note_id}", "NOT_FOUND"), json_mode=True)
        else:
            console.print(f"[red]Not found:[/] {note_id}")
        raise typer.Exit(1)
    if row["origin"] == "adopted":
        msg = "Adopted canonical files cannot be moved by Hyperresearch note commands."
        if json_output:
            output(error(msg, "ADOPTED_READ_ONLY"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)

    old_file = vault.root / row["path"]

    # Resolve the destination into something sync will see. Joined verbatim, a
    # bare name landed at the vault root with no suffix, where sync never
    # looks, so the note silently left the index; and rename() replaced an
    # existing note file without a word. research/index/ is synced but not a
    # destination: IndexGenerator.build_all() deletes every .md there on the
    # next repair, so a note moved in would be lost.
    dest = Path(new_path)
    if dest.name and dest.suffix.lower() != ".md":
        dest = dest.with_name(dest.name + ".md")
    if dest.parent == Path("."):
        dest = vault.notes_dir.relative_to(vault.root) / dest
    new_file = vault.root / dest
    synced_roots = (vault.notes_dir, vault.temp_dir)
    if not any(_under(new_file, r) and new_file.resolve() != r.resolve() for r in synced_roots):
        allowed = ", ".join(r.relative_to(vault.root).as_posix() + "/" for r in synced_roots)
        msg = f"Destination {dest.as_posix()} is outside the synced tree ({allowed})"
        if json_output:
            output(error(msg, "OUTSIDE_SYNCED_TREE"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)
    # samefile: on a case-insensitive filesystem a case-only rename
    # (n-f -> N-F.md) "exists" because it is the note's own file.
    if new_file.exists() and not new_file.samefile(old_file):
        msg = f"Destination already exists: {dest.as_posix()}"
        if json_output:
            output(error(msg, "DESTINATION_EXISTS"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)
    # Resolve the parent only: on a case-insensitive filesystem resolve() on the
    # file itself returns the old spelling during a case-only rename.
    rel_new = (
        (new_file.parent.resolve() / new_file.name).relative_to(vault.root.resolve()).as_posix()
    )

    new_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.rename(new_file)

    # Re-sync
    plan = compute_sync_plan(vault, force=True)
    execute_sync(vault, plan)

    if json_output:
        output(success({"old_path": row["path"], "new_path": rel_new}, vault=str(vault.root)), json_mode=True)
    else:
        console.print(f"[green]Moved:[/] {row['path']} → {rel_new}")
