"""Install-time prompt augmentation for reference-based adopted corpora."""

from __future__ import annotations

import re

_FRONTMATTER_NAME = re.compile(r"^name:\s*([^\n]+)$", re.MULTILINE)


def _skill_name(content: str) -> str | None:
    match = _FRONTMATTER_NAME.search(content)
    return match.group(1).strip() if match else None


def adoption_guidance(content: str) -> str | None:
    """Return adoption guidance for the entry router and the two early steps."""
    name = _skill_name(content)
    if name == "hyperresearch":
        return """## Adopted-corpus baseline (installed)

Before step 1, run `hyperresearch adopt-status --json`.

If exactly one adopted corpus is an unmistakable match for the user's named
project/domain, or the user explicitly names its tag, use it as **pre-existing
project context**. If several could match and none is clearly intended, do not
guess.

Run 2-4 short targeted `hyperresearch search "<terms>" --tag <tag> --limit 12
--json` queries against that corpus. Write
`research/runs/<vault_tag>/temp/adopted-baseline.md` with the corpus tag,
canonical root, selected note ids/titles/canonical paths/snippets, and only
those settled/open/superseded boundaries that the adopted documents themselves
state.

Adopted material is context only:
- never copy canonical adopted Markdown into `research/notes/`;
- never mark a V8 step complete because old project work exists;
- never count adopted hits toward this run's fresh-source quota or spend;
- never let adopted context override the user's explicit current question.

Record the adopted tag and baseline path in the run scaffold. The purpose is to
avoid rediscovering settled project knowledge while preserving the normal V8
research and verification contract for the new question.
"""
    if name == "hyperresearch-1-decompose":
        return """## Adopted project context (installed)

If `research/runs/<vault_tag>/temp/adopted-baseline.md` exists, read it
immediately after the canonical query. Distinguish the user's current question
from project background established before this run. Use explicit
settled/open/superseded boundaries to avoid decomposing the project as if it
had no history, but do not treat adopted material as evidence that this run
already completed any pipeline stage.

When updating the scaffold, name the adopted corpus tag and baseline path and
record only materially relevant boundaries supported by the adopted documents.
"""
    if name == "hyperresearch-2-width-sweep":
        return """## Adopted baseline during width sweep (installed)

If `research/runs/<vault_tag>/temp/adopted-baseline.md` exists, use it to avoid
searches whose only purpose would be to rediscover explicitly settled project
decisions. Search the user's unresolved question and any evidence needed to
test it. Adopted baseline documents do **not** count toward this run's
fresh-source targets or minimum independent-source counts unless this run
separately fetches/ingests the underlying source under its own `vault_tag`.
"""
    return None
