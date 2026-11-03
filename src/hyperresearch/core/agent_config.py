"""Model-agent runtime configuration.

Kept separate from the larger vault configuration module so runtime/provider
routing can evolve independently without bloating the core TOML serializer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    """Model-agent execution backend ([agents] section).

    runtime=native preserves the host's existing Codex/Claude spawn path.
    runtime=pi launches the same Hyperresearch role prompts through Pi;
    provider and model select Pi's model provider independently from the
    research pipeline. Credentials never belong here.
    """

    runtime: str = "native"  # native | pi
    provider: str = ""
    model: str = ""
    effort: str = "low"
    command: str = "pi"
    timeout_s: int = 900
