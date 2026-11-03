"""Post-install runtime dispatch injection for generated Hyperresearch skills.

The legacy installer/render machinery lives in a very large module. Runtime
selection is kept here as a small, idempotent post-processing seam so the same
installed pipeline can choose native Codex/Claude agents or Pi-backed agents
without forking the skill templates.
"""

from __future__ import annotations

from pathlib import Path

from hyperresearch.core.platforms import CLAUDE, CODEX, check_platform
from hyperresearch.core.render import insert_after_frontmatter

_MARKER = "## Agent runtime dispatch (installed)"


def runtime_dispatch_text(hpr_path: str) -> str:
    """Return the runtime-switch contract injected into generated procedures."""
    hpr = hpr_path.replace("\\", "/")
    return f"""## Agent runtime dispatch (installed)

Before this step launches any Hyperresearch subagent, run `{hpr} agent show -j`.

- `runtime: native`: follow this step's existing Codex/Claude spawn instructions exactly.
- `runtime: pi`: do **not** invoke a native Task/custom-agent spawn. Build the exact same
  spawn prompt the step specifies, save it under this run's `temp/` directory, then run
  `{hpr} agent run <role> --prompt-file <path> -j`. The command loads the same installed
  Hyperresearch role prompt and uses the configured Pi provider/model.
- When this step requires several agents in parallel under Pi, write a JSON array of
  `{{"role": "<role>", "prompt_file": "<path>"}}` objects and run
  `{hpr} agent batch <tasks.json> -j`. One failed task fails the batch; requests are never
  automatically retried or silently moved to another model/provider.
- Switching runtime never changes `vault_tag`, source notes, step outputs, or the run manifest.
  A Pi-backed role satisfies the same role/exit criterion; it is not a second pipeline.
- The Claude browser-fetcher remains native-only. Under Pi, leave browser escalations queued
  for the human instead of bypassing that boundary.
"""


def inject_runtime_dispatch(path: Path, hpr_path: str) -> bool:
    """Inject the dispatch block into one generated skill/step if needed."""
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8")
    if _MARKER in content:
        return False
    updated = insert_after_frontmatter(content, runtime_dispatch_text(hpr_path).strip())
    path.write_text(updated, encoding="utf-8")
    return True


def install_runtime_dispatch(root: Path, hpr_path: str, platform: str) -> list[str]:
    """Inject dispatch into every generated entry/step procedure for a platform."""
    platform = check_platform(platform)
    candidates: list[Path] = []

    if platform == CLAUDE:
        skills_root = root / ".claude" / "skills"
        candidates.append(skills_root / "hyperresearch" / "SKILL.md")
        candidates.extend(sorted(skills_root.glob("hyperresearch-*/SKILL.md")))
    elif platform == CODEX:
        candidates.append(root / ".agents" / "skills" / "hyperresearch" / "SKILL.md")
        candidates.extend(sorted((root / ".hyperresearch" / "codex" / "steps").glob("*.md")))

    changed = [path for path in candidates if inject_runtime_dispatch(path, hpr_path)]
    if not changed:
        return []
    return [
        f"{platform}: runtime dispatch injected into {len(changed)} generated procedure(s)"
    ]
