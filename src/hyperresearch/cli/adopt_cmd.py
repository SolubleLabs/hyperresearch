"""CLI commands for reference-based adoption of existing project knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from hyperresearch.core.adopt import AdoptResult

from hyperresearch.cli._output import console, output
from hyperresearch.models.output import error, success


def _result_data(result: AdoptResult) -> dict[str, object]:
    return {
        "tag": result.tag,
        "root_path": result.root_path,
        "count": result.count,
        "added": result.added,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "removed": result.removed,
        "items": [
            {
                "canonical_path": item.canonical_path,
                "id": item.note_id,
                "action": item.action,
                "kind": item.kind,
                "content_hash": item.content_hash,
            }
            for item in result.items
        ],
        "removed_ids": result.removed_ids,
    }


def adopt(
    source: str = typer.Argument(..., help="Canonical Markdown file or directory to index in place"),
    tag: str = typer.Option(..., "--tag", "-t", help="Stable corpus tag, e.g. anc"),
    kind: str = typer.Option("auto", "--kind", help="Artifact classification; default auto"),
    keep_missing: bool = typer.Option(
        False,
        "--keep-missing",
        help="Keep indexed rows when a previously adopted canonical file is absent",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview index changes without writing"),
    rebind: bool = typer.Option(
        False,
        "--rebind",
        help="Intentionally repoint this tag to a moved copy of the same corpus root",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Index existing Markdown without copying or rewriting canonical files."""
    from hyperresearch.core.adopt import AdoptError, adopt_path
    from hyperresearch.core.vault import Vault, VaultError

    try:
        vault = Vault.discover()
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        result = adopt_path(
            vault,
            source_path,
            tag=tag,
            kind=kind,
            prune=not keep_missing,
            dry_run=dry_run,
            rebind=rebind,
        )
    except (AdoptError, VaultError, UnicodeDecodeError, OSError) as exc:
        if json_output:
            output(error(str(exc), "ADOPT_ERROR"), json_mode=True)
        else:
            console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1) from exc

    data = _result_data(result)
    if json_output:
        output(success(data, count=result.count, vault=str(vault.root)), json_mode=True)
        return

    mode = "Would adopt/refresh" if dry_run else "Adopted/refreshed"
    console.print(
        f"[green]{mode}[/] corpus [cyan]{result.tag}[/]: "
        f"{result.added} added, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.removed} no-longer-present"
    )
    console.print(f"  canonical root: {result.root_path}")
    console.print("  canonical Markdown remains in place; research/notes/ receives no copies")


def adopt_status(
    tag: str | None = typer.Argument(None, help="Optional adoption tag to inspect"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """List adopted corpora and indexed artifact counts."""
    from hyperresearch.core.vault import Vault, VaultError

    try:
        vault = Vault.discover()
    except VaultError as exc:
        if json_output:
            output(error(str(exc), "NO_VAULT"), json_mode=True)
        else:
            console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1) from exc

    params: list[object] = []
    where = ""
    if tag is not None:
        where = "WHERE ar.tag = ?"
        params.append(tag)

    rows = vault.db.execute(
        f"""
        SELECT
            ar.tag,
            ar.root_path,
            ar.kind_mode,
            ar.created_at,
            ar.synced_at,
            COUNT(n.id) AS note_count
        FROM adoption_roots ar
        LEFT JOIN notes n
          ON n.origin='adopted' AND n.adopt_group=ar.tag
        {where}
        GROUP BY ar.tag, ar.root_path, ar.kind_mode, ar.created_at, ar.synced_at
        ORDER BY ar.tag
        """,
        params,
    ).fetchall()

    corpora: list[dict[str, object]] = []
    for row in rows:
        kinds = {
            kind_row["adopt_kind"]: kind_row["n"]
            for kind_row in vault.db.execute(
                """
                SELECT adopt_kind, COUNT(*) AS n
                FROM notes
                WHERE origin='adopted' AND adopt_group=?
                GROUP BY adopt_kind
                ORDER BY adopt_kind
                """,
                (row["tag"],),
            )
        }
        corpora.append(
            {
                "tag": row["tag"],
                "root_path": row["root_path"],
                "kind_mode": row["kind_mode"],
                "created_at": row["created_at"],
                "synced_at": row["synced_at"],
                "note_count": row["note_count"],
                "kinds": kinds,
            }
        )

    if tag is not None and not corpora:
        msg = f"No adopted corpus registered with tag {tag!r}."
        if json_output:
            output(error(msg, "NOT_FOUND"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)

    if json_output:
        output(success(corpora, count=len(corpora), vault=str(vault.root)), json_mode=True)
        return

    if not corpora:
        console.print("[dim]No adopted corpora registered.[/]")
        return

    for corpus in corpora:
        console.print(
            f"[cyan]{corpus['tag']}[/] — {corpus['note_count']} indexed files — "
            f"{corpus['root_path']}"
        )


def adopt_sync(
    tag: str = typer.Argument(..., help="Previously registered adoption tag"),
    keep_missing: bool = typer.Option(
        False,
        "--keep-missing",
        help="Keep indexed rows when a previously adopted canonical file is absent",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview refresh changes without writing"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Refresh a registered adopted corpus from its canonical paths."""
    from hyperresearch.core.adopt import AdoptError, sync_adoption
    from hyperresearch.core.vault import Vault, VaultError

    try:
        vault = Vault.discover()
        result = sync_adoption(
            vault,
            tag,
            prune=not keep_missing,
            dry_run=dry_run,
        )
    except (AdoptError, VaultError, UnicodeDecodeError, OSError) as exc:
        if json_output:
            output(error(str(exc), "ADOPT_ERROR"), json_mode=True)
        else:
            console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1) from exc

    data = _result_data(result)
    if json_output:
        output(success(data, count=result.count, vault=str(vault.root)), json_mode=True)
        return

    mode = "Would refresh" if dry_run else "Refreshed"
    console.print(
        f"[green]{mode}[/] corpus [cyan]{result.tag}[/]: "
        f"{result.added} added, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.removed} no-longer-present"
    )
