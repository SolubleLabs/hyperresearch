from __future__ import annotations

from hyperresearch.core.render import insert_after_frontmatter


def _install(content: str) -> str:
    return insert_after_frontmatter(
        content,
        '<!-- rendered from profile "full" (hyperresearch test) -->',
    )


def test_entry_skill_gets_adopted_corpus_guidance():
    rendered = _install("---\nname: hyperresearch\n---\n\n# Router\n")
    assert "## Adopted-corpus baseline (installed)" in rendered
    assert "hyperresearch adopt-status --json" in rendered
    assert "never mark a V8 step complete" in rendered


def test_early_steps_get_role_specific_adoption_guidance():
    decompose = _install("---\nname: hyperresearch-1-decompose\n---\n\n# Step\n")
    width = _install("---\nname: hyperresearch-2-width-sweep\n---\n\n# Step\n")
    assert "## Adopted project context (installed)" in decompose
    assert "## Adopted baseline during width sweep (installed)" in width
    assert "fresh-source targets" in width


def test_other_skills_and_non_install_injections_are_unchanged():
    other = _install("---\nname: hyperresearch-8-corpus-critic\n---\n\n# Step\n")
    runtime = insert_after_frontmatter(
        "---\nname: hyperresearch\n---\n\n# Router\n",
        "## Agent runtime dispatch (installed)",
    )
    assert "Adopted-corpus baseline" not in other
    assert "Adopted-corpus baseline" not in runtime
    assert runtime.count("Agent runtime dispatch") == 1
