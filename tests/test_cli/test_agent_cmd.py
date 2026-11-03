from __future__ import annotations

import json

from typer.testing import CliRunner

import hyperresearch.cli.agent_cmd as agent_cmd
from hyperresearch.cli import app
from hyperresearch.core.agent_runtime import AgentRunResult
from hyperresearch.core.config import AgentSettings, VaultConfig

runner = CliRunner()


def _data(result):
    return json.loads(result.stdout)["data"]


def test_agent_runtime_switch_preserves_pi_selection(tmp_vault, monkeypatch) -> None:
    monkeypatch.chdir(tmp_vault.root)
    home = tmp_vault.root / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    entry = tmp_vault.root / ".agents" / "skills" / "hyperresearch" / "SKILL.md"
    entry.parent.mkdir(parents=True)
    entry.write_text("---\nname: hyperresearch\n---\n\n# Router\n", encoding="utf-8")

    selected = runner.invoke(
        app,
        [
            "agent",
            "use",
            "pi",
            "--provider",
            "tokenme",
            "--model",
            "google/gemma-4-31b-it",
            "--effort",
            "low",
            "--command",
            "F:/tools/pi.CMD",
            "--json",
        ],
    )
    assert selected.exit_code == 0, selected.stdout
    assert _data(selected)["runtime"] == "pi"
    assert _data(selected)["provider"] == "tokenme"
    assert _data(selected)["model"] == "google/gemma-4-31b-it"
    assert _data(selected)["run_artifacts_changed"] is False
    assert _data(selected)["runtime_dispatch"]
    assert "## Agent runtime dispatch (installed)" in entry.read_text(encoding="utf-8")

    native = runner.invoke(app, ["agent", "use", "native", "--json"])
    assert native.exit_code == 0, native.stdout
    assert _data(native)["runtime"] == "native"

    config = VaultConfig.load(tmp_vault.config_path)
    assert config.agents.runtime == "native"
    assert config.agents.provider == "tokenme"
    assert config.agents.model == "google/gemma-4-31b-it"

    back = runner.invoke(app, ["agent", "use", "pi", "--json"])
    assert back.exit_code == 0, back.stdout
    assert _data(back)["runtime"] == "pi"
    assert _data(back)["provider"] == "tokenme"
    assert _data(back)["model"] == "google/gemma-4-31b-it"
    assert _data(back)["runtime_dispatch"] == []


def test_agent_show_never_reports_credentials(tmp_vault, monkeypatch) -> None:
    monkeypatch.chdir(tmp_vault.root)
    shown = runner.invoke(app, ["agent", "show", "--json"])
    assert shown.exit_code == 0, shown.stdout
    body = shown.stdout.lower()
    assert "api_key" not in body
    assert "bearer" not in body
    assert _data(shown)["runtime"] == "native"


def test_agent_batch_preserves_task_order_without_retries(tmp_vault, monkeypatch) -> None:
    monkeypatch.chdir(tmp_vault.root)
    tmp_vault.config.agents = AgentSettings(
        runtime="pi",
        provider="tokenme",
        model="google/gemma-4-31b-it",
        command="pi",
    )
    tmp_vault.config.save(tmp_vault.config_path)

    first = tmp_vault.root / "first.md"
    second = tmp_vault.root / "second.md"
    first.write_text("first prompt", encoding="utf-8")
    second.write_text("second prompt", encoding="utf-8")
    tasks = tmp_vault.root / "tasks.json"
    tasks.write_text(
        json.dumps(
            [
                {"role": "depth-critic", "prompt_file": "first.md"},
                {"role": "width-critic", "prompt_file": "second.md"},
            ]
        ),
        encoding="utf-8",
    )

    def fake_run_pi_agent(*, vault_root, role, prompt, settings):
        assert vault_root == tmp_vault.root
        assert settings.provider == "tokenme"
        return AgentRunResult(
            role=role,
            runtime="pi",
            provider=settings.provider,
            model=settings.model,
            effort=settings.effort,
            response=f"{role}:{prompt}",
        )

    monkeypatch.setattr(agent_cmd, "run_pi_agent", fake_run_pi_agent)
    result = runner.invoke(app, ["agent", "batch", str(tasks), "--json"])
    assert result.exit_code == 0, result.stdout
    data = _data(result)
    assert data["completed"] == 2
    assert data["failed"] == 0
    assert data["retries"] == 0
    assert [item["role"] for item in data["results"]] == ["depth-critic", "width-critic"]
    assert [item["response"] for item in data["results"]] == [
        "depth-critic:first prompt",
        "width-critic:second prompt",
    ]
