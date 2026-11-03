"""Runtime-neutral Hyperresearch agent backend commands."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from hyperresearch.cli._output import console, output
from hyperresearch.core.agent_runtime import (
    AgentRunResult,
    AgentRuntimeError,
    read_batch_tasks,
    run_pi_agent,
    validate_agent_settings,
)
from hyperresearch.models.output import error, success

if TYPE_CHECKING:
    from hyperresearch.core.vault import Vault

app = typer.Typer()


def _vault() -> Vault:
    from hyperresearch.core.vault import Vault

    return Vault.discover()


def _prompt_text(vault_root: Path, prompt: str | None, prompt_file: str | None) -> str:
    if (prompt is None) == (prompt_file is None):
        raise AgentRuntimeError("Provide exactly one of --prompt or --prompt-file.")
    if prompt is not None:
        text = prompt.strip()
        if not text:
            raise AgentRuntimeError("Agent prompt cannot be empty.")
        return text
    assert prompt_file is not None
    path = Path(prompt_file)
    if not path.is_absolute():
        path = vault_root / path
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentRuntimeError(f"Could not read agent prompt file: {path}") from exc
    if not text:
        raise AgentRuntimeError(f"Agent prompt file is empty: {path}")
    return text


@app.command("show")
def agent_show(
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Show the selected model-agent runtime. Never displays credentials."""
    vault = _vault()
    settings = vault.config.agents
    data = {
        "runtime": settings.runtime,
        "provider": settings.provider or None,
        "model": settings.model or None,
        "effort": settings.effort,
        "command": settings.command,
        "timeout_s": settings.timeout_s,
        "credentials": "owned by selected provider/runtime; not stored by Hyperresearch",
    }
    if json_output:
        output(success(data, vault=str(vault.root)), json_mode=True)
        return
    console.print("[bold]Hyperresearch agent runtime[/]")
    for key, value in data.items():
        console.print(f"  [dim]{key}:[/] {value}")


@app.command("use")
def agent_use(
    runtime: str = typer.Argument(..., help="native or pi"),
    provider: str | None = typer.Option(None, "--provider", help="Pi model provider"),
    model: str | None = typer.Option(None, "--model", help="Model id inside the Pi provider"),
    effort: str | None = typer.Option(None, "--effort", help="Pi thinking level"),
    command: str | None = typer.Option(None, "--command", help="Pi executable path/name"),
    timeout_s: int | None = typer.Option(None, "--timeout", min=1, help="Per-agent timeout seconds"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Switch agent runtime without changing the research run or its artifacts."""
    vault = _vault()
    previous = vault.config.agents
    normalized = runtime.strip().lower()
    if normalized not in {"native", "pi"}:
        message = "Agent runtime must be native or pi."
        if json_output:
            output(error(message, "INVALID_AGENT_RUNTIME"), json_mode=True)
        else:
            console.print(f"[red]Error:[/] {message}")
        raise typer.Exit(1)

    next_settings = replace(
        previous,
        runtime=normalized,
        provider=provider.strip() if provider is not None else previous.provider,
        model=model.strip() if model is not None else previous.model,
        effort=effort.strip() if effort is not None else previous.effort,
        command=command.strip() if command is not None else previous.command,
        timeout_s=timeout_s if timeout_s is not None else previous.timeout_s,
    )
    try:
        validate_agent_settings(next_settings)
    except AgentRuntimeError as exc:
        if json_output:
            output(error(str(exc), "INVALID_AGENT_RUNTIME_CONFIG"), json_mode=True)
        else:
            console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    vault.config.agents = next_settings
    vault.config.save(vault.config_path)

    dispatch_actions: list[str] = []
    if normalized == "pi":
        from hyperresearch.core.agent_docs import _resolve_executable
        from hyperresearch.core.agent_runtime_install import install_runtime_dispatch
        from hyperresearch.core.hooks import installed_platforms

        hpr_path = _resolve_executable()
        for root in (vault.root, Path.home()):
            for platform in installed_platforms(root):
                dispatch_actions += install_runtime_dispatch(root, hpr_path, platform)

    data = {
        "runtime": next_settings.runtime,
        "provider": next_settings.provider or None,
        "model": next_settings.model or None,
        "effort": next_settings.effort,
        "command": next_settings.command,
        "timeout_s": next_settings.timeout_s,
        "run_artifacts_changed": False,
        "runtime_dispatch": dispatch_actions,
    }
    if json_output:
        output(success(data, vault=str(vault.root)), json_mode=True)
    else:
        console.print(
            f"[green]Agent runtime:[/] {next_settings.runtime}"
            + (
                f" ({next_settings.provider}/{next_settings.model}, {next_settings.effort})"
                if next_settings.runtime == "pi"
                else ""
            )
        )


@app.command("run")
def agent_run(
    role: str = typer.Argument(..., help="Hyperresearch role, e.g. corpus-critic"),
    prompt: str | None = typer.Option(None, "--prompt", help="Exact spawn prompt"),
    prompt_file: str | None = typer.Option(None, "--prompt-file", help="UTF-8 file with exact spawn prompt"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Run one role through the configured non-native backend."""
    vault = _vault()
    settings = vault.config.agents
    if settings.runtime == "native":
        message = (
            "agents.runtime is native. Use the current Codex/Claude host's normal "
            "subagent mechanism; Hyperresearch does not proxy native credentials."
        )
        if json_output:
            output(error(message, "NATIVE_AGENT_RUNTIME"), json_mode=True)
        else:
            console.print(f"[yellow]{message}[/]")
        raise typer.Exit(2)
    try:
        result = run_pi_agent(
            vault_root=vault.root,
            role=role,
            prompt=_prompt_text(vault.root, prompt, prompt_file),
            settings=settings,
        )
    except AgentRuntimeError as exc:
        if json_output:
            output(error(str(exc), "AGENT_RUNTIME_ERROR"), json_mode=True)
        else:
            console.print(f"[red]Agent failed:[/] {exc}")
        raise typer.Exit(1)

    data = result.as_dict()
    if json_output:
        output(success(data, vault=str(vault.root)), json_mode=True)
    else:
        typer.echo(result.response)


@app.command("batch")
def agent_batch(
    tasks_file: str = typer.Argument(..., help="JSON array of role/prompt_file tasks"),
    max_workers: int = typer.Option(0, "--max-workers", min=0, max=16, help="0 = one worker per task"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Run a native-parallel Hyperresearch spawn group through Pi, with no retries."""
    vault = _vault()
    settings = vault.config.agents
    if settings.runtime == "native":
        message = "agents.runtime is native; use the host's normal parallel subagent spawn."
        if json_output:
            output(error(message, "NATIVE_AGENT_RUNTIME"), json_mode=True)
        else:
            console.print(f"[yellow]{message}[/]")
        raise typer.Exit(2)

    path = Path(tasks_file)
    if not path.is_absolute():
        path = vault.root / path
    try:
        tasks = read_batch_tasks(path)
        workers = max_workers or len(tasks)
        results: list[dict[str, object] | None] = [None] * len(tasks)
        failures: list[dict[str, str]] = []

        def execute(index: int, task: dict[str, str]) -> tuple[int, AgentRunResult]:
            prompt_text = _prompt_text(vault.root, None, task["prompt_file"])
            return index, run_pi_agent(
                vault_root=vault.root,
                role=task["role"],
                prompt=prompt_text,
                settings=settings,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(execute, index, task) for index, task in enumerate(tasks)]
            for future in as_completed(futures):
                try:
                    index, result = future.result()
                    results[index] = result.as_dict()
                except AgentRuntimeError as exc:
                    failures.append({"error": str(exc)})

        data = {
            "runtime": settings.runtime,
            "provider": settings.provider,
            "model": settings.model,
            "task_count": len(tasks),
            "completed": sum(item is not None for item in results),
            "failed": len(failures),
            "results": results,
            "failures": failures,
            "retries": 0,
        }
        if failures:
            if json_output:
                output(error("One or more agent tasks failed. " + str(data), "AGENT_BATCH_FAILED"), json_mode=True)
            else:
                console.print(f"[red]Agent batch failed:[/] {len(failures)} of {len(tasks)}")
            raise typer.Exit(1)
    except AgentRuntimeError as exc:
        if json_output:
            output(error(str(exc), "AGENT_RUNTIME_ERROR"), json_mode=True)
        else:
            console.print(f"[red]Agent batch failed:[/] {exc}")
        raise typer.Exit(1)

    if json_output:
        output(success(data, vault=str(vault.root)), json_mode=True)
    else:
        for item in results:
            assert item is not None
            typer.echo(item["response"])
