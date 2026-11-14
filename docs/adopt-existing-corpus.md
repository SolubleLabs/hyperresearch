# Adopt existing project corpora

`hyperresearch import` copies Markdown into the vault. `hyperresearch adopt` is for mature
projects whose canonical Markdown must stay in its owning project tree.

## Contract

- Canonical project files stay in place; Hyperresearch stores indexed content, canonical paths, hashes, and classifications in SQLite/FTS.
- Adopted notes are read-only through Hyperresearch mutation commands. Edit the owning project file, then run `hyperresearch adopt-sync <tag>`.
- Native `hyperresearch sync` ignores adopted rows; adopted corpora refresh only through the adoption workflow.
- `--rebind` moves an adoption root to another checkout/worktree while preserving note identities by corpus-relative path.
- Adoption is baseline context, never retroactive V8 step credit. It cannot satisfy fresh-source quotas or mark historical pipeline steps complete.

## Commands

```bash
hyperresearch adopt docs/anc --tag anc
hyperresearch adopt-status anc -j
hyperresearch search "agenda explainability" --tag anc --limit 12 -j
hyperresearch adopt-sync anc
hyperresearch adopt /new/worktree/docs/anc --tag anc --rebind
```

Use `--dry-run` on `adopt` or `adopt-sync` to inspect additions, updates, removals, and unchanged documents without changing the index.

## Safety

Adopted rows use virtual note paths for search/index identity; those paths are not writable files under the vault. Single-note edit/update/move/remove and every batch mutator must fail with `ADOPTED_READ_ONLY` before any write. Mixed native+adopted batches must fail atomically rather than partially mutating native notes.

## Pipeline use

When the installed `$hyperresearch`/`/hyperresearch` router sees a clearly relevant adopted corpus, it may build `research/runs/<vault_tag>/temp/adopted-baseline.md` from a few targeted searches. That baseline records only project boundaries supported by the adopted documents and constrains the new question. The normal V8 research and verification contract still applies.

Use adoption to prevent rediscovery of settled project work, not to turn Hyperresearch infrastructure into the project objective.
