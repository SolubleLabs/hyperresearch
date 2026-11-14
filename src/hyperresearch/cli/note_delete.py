"""Note deletion and asset cleanup.

Deletion has a wider filesystem safety surface than ordinary frontmatter edits,
so it is isolated from the other mutation commands.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import typer

from hyperresearch.cli._output import console, output
from hyperresearch.models.output import error, success


def _file_identity(path: Path) -> tuple | None:
    """What makes `path` the same file as another spelling of it, or None
    when no regular file is there.

    The device and inode, so case, symlinks and `..` do not matter.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_dev, st.st_ino)


def _located(path: Path) -> Path:
    """`path` with its directory resolved; a symlink stays where it sits."""
    return path.parent.resolve() / path.name


def _is_below(path: Path, root: Path) -> bool:
    """Whether `path`, symlinks resolved, lies strictly below the directory `root`."""
    where, root = path.resolve(), root.resolve()
    return where != root and where.is_relative_to(root)


def note_rm(
    note_id: str = typer.Argument(..., help="Note ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Delete a note and its associated raw file and assets.

    Previous versions only unlinked the `.md` file, leaving raw PDFs under
    `research/raw/` and assets under `research/assets/<id>/` as orphans.
    Every fetch-then-delete cycle leaked disk. The current implementation
    also removes:
      - the raw file referenced in the note's `raw_file` frontmatter field
      - the files under `research/assets/` that the note's rows in the
        `assets` table name, and the rest of `research/assets/<id>/` unless
        that is a symlink, except a file another note's row names; nothing
        outside `research/assets/` is removed
    A file that cannot be removed does not stop the delete; it is reported
    under `assets_not_removed`.
    """
    from hyperresearch.core.vault import Vault

    vault = Vault.discover()
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
            "Adopted canonical files cannot be deleted by Hyperresearch note commands. "
            "Remove the canonical file in its owning project, then refresh the adoption."
        )
        if json_output:
            output(error(msg, "ADOPTED_READ_ONLY"), json_mode=True)
        else:
            console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)

    file_path = vault.root / row["path"]
    if not force and not json_output:
        typer.confirm(f"Delete {row['path']}?", abort=True)

    removed_raw: str | None = None
    removed_assets: list[str] = []

    # Read raw_file straight from the markdown frontmatter — the DB row
    # may not carry that field if the schema was synced before the column
    # existed. File parsing is authoritative.
    if file_path.exists():
        try:
            from hyperresearch.core.frontmatter import parse_frontmatter
            text = file_path.read_text(encoding="utf-8-sig")
            meta, _ = parse_frontmatter(text)
            if meta.raw_file:
                # Resolve and guard against path traversal — a malicious or
                # corrupted frontmatter could set raw_file to "../../etc/passwd".
                # Refuse to unlink anything outside <vault>/research/.
                research_root = (vault.root / "research").resolve()
                raw_path = (vault.root / "research" / meta.raw_file).resolve()
                try:
                    raw_path.relative_to(research_root)
                    inside_vault = True
                except ValueError:
                    inside_vault = False
                if inside_vault and raw_path.exists() and raw_path.is_file():
                    raw_path.unlink()
                    removed_raw = str(raw_path.relative_to(vault.root).as_posix())
        except Exception:
            pass

    # Assets. A row names its file by full path, and the file need not sit
    # under research/assets/<note_id>/: fetch saves a note's files under the
    # stem of its file at the time, and sync renames a row in place when the
    # file's `id:` is rewritten, leaving the files where they are. So the
    # files to remove are the ones this note's own rows name anywhere under
    # research/assets/, plus whatever research/assets/<note_id>/ holds that
    # no other note's row names (fetch can write a file it records no row
    # for). A file another note's row names stays, in whichever directory it
    # is. Files are compared by identity, not by spelling. Nothing is removed
    # whose path, symlinks resolved, is not below this vault's
    # research/assets/: a row naming a file elsewhere (a copy of the vault
    # still holds the original's paths) is reported and its file left alone.
    assets_root = vault.root / "research" / "assets"
    assets_dir = assets_root / note_id
    own_files: list[Path] = []
    others: set[tuple] = set()
    # Where other notes' rows point, as spelled: a symlink a row names is
    # that note's, whatever it points at.
    other_paths: set[Path] = set()
    for r in vault.db.execute("SELECT note_id, filename FROM assets"):
        if r["note_id"] == note_id:
            own_files.append(Path(r["filename"]))
        else:
            other_paths.add(_located(Path(r["filename"])))
            ident = _file_identity(Path(r["filename"]))
            if ident is not None:
                others.add(ident)
    seen: set[tuple] = set()
    seen_links: set[Path] = set()
    assets_not_removed: list[str] = []
    # The directories a removal may have emptied.
    dirs: set[Path] = set()

    def _label(path: Path) -> str:
        # Vault-relative; a symlink is named where it sits, not by its target.
        try:
            return _located(path).relative_to(vault.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _remove_link(path: Path) -> None:
        # A symlink is removed as itself, dangling or not: unlinking it never
        # touches its target, wherever that is. Only a link that sits below
        # research/assets/ goes, and not one another note's row names.
        where = _located(path)
        if where in other_paths or where in seen_links:
            return
        seen_links.add(where)
        if not where.parent.is_relative_to(assets_root.resolve()):
            assets_not_removed.append(f"{path}: outside research/assets/, left in place")
            return
        label = _label(path)
        try:
            path.unlink()
        except OSError as exc:
            assets_not_removed.append(f"{label}: {exc.strerror or exc}")
            return
        removed_assets.append(label)
        dirs.add(path.parent)

    def _remove_file(path: Path) -> None:
        if path.is_symlink():
            _remove_link(path)
            return
        ident = _file_identity(path)
        if ident is None or ident in others or ident in seen:
            return
        seen.add(ident)
        if not _is_below(path, assets_root):
            assets_not_removed.append(f"{path}: outside research/assets/, left in place")
            return
        label = _label(path)
        try:
            path.unlink()
        except OSError as exc:
            assets_not_removed.append(f"{label}: {exc.strerror or exc}")
            return
        removed_assets.append(label)
        dirs.add(path.parent)

    for path in own_files:
        _remove_file(path)
    # A symlinked research/assets/<note_id> points at a directory that is not
    # this note's to empty or remove, inside the vault or not; the link stays,
    # as before.
    if assets_dir.is_dir() and not assets_dir.is_symlink():
        dirs.add(assets_dir)
        for path in sorted(assets_dir.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                dirs.add(path)
            else:
                _remove_file(path)
    # An empty one goes, deepest first: never a symlink, and only below
    # research/assets/.
    for d in sorted({_located(d) for d in dirs}, key=lambda d: len(d.parts), reverse=True):
        if d.is_symlink() or not _is_below(d, assets_root):
            continue
        try:
            if not d.is_dir() or any(d.iterdir()):
                continue
            d.rmdir()
        except OSError as exc:
            assets_not_removed.append(f"{_label(d)}/: {exc.strerror or exc}")

    # Finally, unlink the .md file
    if file_path.exists():
        file_path.unlink()

    # Re-sync to update DB
    from hyperresearch.core.sync import compute_sync_plan, execute_sync

    plan = compute_sync_plan(vault)
    execute_sync(vault, plan)

    payload: dict = {"deleted": note_id}
    if removed_raw:
        payload["removed_raw"] = removed_raw
    if removed_assets:
        payload["removed_assets"] = removed_assets
    if assets_not_removed:
        payload["assets_not_removed"] = assets_not_removed

    if json_output:
        output(success(payload, vault=str(vault.root)), json_mode=True)
    else:
        msg = f"[red]Deleted:[/] {note_id}"
        if removed_raw:
            msg += f"\n  raw: {removed_raw}"
        if removed_assets:
            msg += f"\n  assets: {len(removed_assets)} file(s)"
        if assets_not_removed:
            msg += "\n  assets not removed: " + "; ".join(assets_not_removed)
        console.print(msg)
