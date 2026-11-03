from __future__ import annotations

from pathlib import Path

import pytest

from hyperresearch.core.agent_runtime import AgentRuntimeError, build_pi_command
from hyperresearch.core.config import AgentSettings, VaultConfig


def test_agent_settings_round_trip_without_secrets(tmp_path: Path) -> None:
    config = VaultConfig(
        agents=AgentSettings(
            runtime="pi",
            provider="tokenme",
            model="google/gemma-4-31b-it",
            effort="low",
            command=r"F:\\tools\\pi.CMD",
            timeout_s=900,
        )
    )
    path = tmp_path / "config.toml"
    config.save(path)

    text = path.read_text(encoding="utf-8")
    assert "[agents]" in text
    assert 'runtime = "pi"' in text
    assert 'provider = "tokenme"' in text
    assert "TOKENME_API_KEY" not in text
    assert "tokenme.limited/api" not in text

    loaded = VaultConfig.load(path)
    assert loaded.agents == config.agents
    assert loaded.agents.command == r"F:\\tools\\pi.CMD"


def test_agent_settings_default_to_native() -> None:
    assert VaultConfig().agents == AgentSettings()


def test_build_pi_command_reuses_installed_agent_prompt(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".claude" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "hyperresearch-corpus-critic.md").write_text(
        "---\nname: hyperresearch-corpus-critic\ntools: Bash, Read, Write, WebSearch\n---\n"
        "Read the supplied corpus and report contradictions.\n",
        encoding="utf-8",
    )
    pi = tmp_path / "pi.exe"
    pi.write_bytes(b"fake")
    settings = AgentSettings(
        runtime="pi",
        provider="tokenme",
        model="google/gemma-4-31b-it",
        effort="low",
        command=str(pi),
    )

    system_prompt_path = tmp_path / "system-prompt.md"
    args, model_ref = build_pi_command(
        vault_root=tmp_path,
        role="corpus-critic",
        settings=settings,
        system_prompt_path=system_prompt_path,
    )

    assert model_ref == "tokenme/google/gemma-4-31b-it"
    assert args[0] == str(pi)
    assert args[args.index("--model") + 1] == model_ref
    assert args[args.index("--tools") + 1] == "bash,read,write"
    assert args[args.index("--system-prompt") + 1] == str(system_prompt_path)
    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    assert "same prompt used by the native Claude agent" in system_prompt
    assert "Read the supplied corpus" in system_prompt
    assert args[-1] == "-p"
    assert "TOKENME_API_KEY" not in "\n".join(args)


def test_build_pi_command_blocks_browser_fetcher(tmp_path: Path) -> None:
    pi = tmp_path / "pi.exe"
    pi.write_bytes(b"fake")
    settings = AgentSettings(
        runtime="pi",
        provider="tokenme",
        model="google/gemma-4-31b-it",
        command=str(pi),
    )
    with pytest.raises(AgentRuntimeError, match="browser-fetcher"):
        build_pi_command(
            vault_root=tmp_path,
            role="browser-fetcher",
            settings=settings,
            system_prompt_path=tmp_path / "system-prompt.md",
        )
