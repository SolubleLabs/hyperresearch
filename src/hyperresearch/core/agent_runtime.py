"""Runtime-neutral Hyperresearch subagent launcher.

Native Codex/Claude spawning stays owned by those hosts. When a vault selects
agents.runtime = pi, this module runs the same installed Hyperresearch agent
prompt through Pi with a model provider chosen independently from the pipeline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from hyperresearch.core.agent_config import AgentSettings


class AgentRuntimeError(RuntimeError):
    """The selected agent backend could not run the requested role."""


@dataclass(frozen=True)
class AgentRunResult:
    role: str
    runtime: str
    provider: str
    model: str
    effort: str
    response: str
    exit_code: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "response": self.response,
            "exit_code": self.exit_code,
        }


_AGENT_PREFIX = "hyperresearch-"
_BLOCKED_PI_ROLES = {"browser-fetcher"}
_TOOL_MAP = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Bash": "bash",
}


def normalize_role(role: str) -> str:
    """Normalize fetcher / hyperresearch-fetcher to the installed suffix."""
    value = role.strip()
    if value.startswith(_AGENT_PREFIX):
        value = value[len(_AGENT_PREFIX):]
    if not value or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise AgentRuntimeError(f"Invalid Hyperresearch agent role: {role!r}")
    return value


def validate_agent_settings(settings: AgentSettings) -> None:
    """Reject incomplete runtime configuration before any model request."""
    if settings.runtime not in {"native", "pi"}:
        raise AgentRuntimeError("agents.runtime must be 'native' or 'pi'.")
    if settings.runtime == "native":
        return
    if not settings.provider.strip():
        raise AgentRuntimeError("agents.provider is required when agents.runtime = 'pi'.")
    if not settings.model.strip():
        raise AgentRuntimeError("agents.model is required when agents.runtime = 'pi'.")
    if not settings.command.strip():
        raise AgentRuntimeError("agents.command is required when agents.runtime = 'pi'.")
    if settings.timeout_s <= 0:
        raise AgentRuntimeError("agents.timeout_s must be greater than zero.")


def resolve_pi_command(settings: AgentSettings) -> str:
    """Resolve the configured Pi executable without guessing a provider secret."""
    command = os.environ.get("HYPERRESEARCH_PI_COMMAND", "").strip() or settings.command.strip()
    candidate = Path(command).expanduser()
    if candidate.is_absolute():
        if not candidate.exists():
            raise AgentRuntimeError(f"Pi command does not exist: {candidate}")
        return str(candidate)
    resolved = shutil.which(command)
    if resolved:
        return resolved
    raise AgentRuntimeError(
        f"Pi command not found: {command}. Set agents.command or HYPERRESEARCH_PI_COMMAND."
    )


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    rest = text[end + 4:]
    newline = rest.find("\n")
    body = rest[newline + 1:] if newline != -1 else ""
    return (meta if isinstance(meta, dict) else {}), body


def _tool_names(meta: dict[str, object]) -> list[str]:
    value = meta.get("tools")
    if isinstance(value, list):
        raw = [str(item).strip() for item in value]
    elif value is None:
        raw = []
    else:
        raw = [item.strip() for item in str(value).split(",")]
    tools = []
    for item in raw:
        mapped = _TOOL_MAP.get(item)
        if mapped and mapped not in tools:
            tools.append(mapped)
    return tools


def locate_agent_markdown(vault_root: Path, role: str) -> Path:
    """Find the Claude-format role prompt reused by the Pi runtime."""
    normalized = normalize_role(role)
    filename = f"{_AGENT_PREFIX}{normalized}.md"
    candidates = [
        vault_root / ".claude" / "agents" / filename,
        Path.home() / ".claude" / "agents" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AgentRuntimeError(
        f"Agent prompt not installed: {filename}. "
        "Run hyperresearch install . --target all before selecting the Pi runtime."
    )


def pi_runtime_preamble(role: str, declared_tools: object) -> str:
    """Translate Claude-agent tool vocabulary to Pi without changing the role body."""
    return f"""## Pi runtime contract

You are running the installed Hyperresearch {_AGENT_PREFIX}{role} role through Pi.
The role body below is the same prompt used by the native Claude agent.

Runtime translations:
- Read -> Pi read.
- Write -> Pi write.
- Edit -> Pi edit.
- Bash -> Pi bash.
- WebSearch -> no direct model web tool. Use Hyperresearch scholar, fetch, and
  fetch-batch through Bash exactly as the research protocol requires.
- Task -> nested agent spawning is not available inside this Pi leaf. When the role
  says to delegate fetches, perform the equivalent bounded fetch/read work yourself.
- ToolSearch / Claude-in-Chrome -> unavailable. Leave browser escalations queued for
  the human instead of bypassing them.

Declared role tools: {declared_tools!s}

Do not claim a native Codex/Claude subagent ran. Do not silently change model providers.
A failed or billed model request is terminal for this invocation; there is no automatic
fallback or retry.
"""


def build_pi_command(
    *,
    vault_root: Path,
    role: str,
    settings: AgentSettings,
    system_prompt_path: Path,
) -> tuple[list[str], str]:
    """Build one Pi invocation and materialize its large system prompt to a file."""
    validate_agent_settings(settings)
    if settings.runtime != "pi":
        raise AgentRuntimeError("agent run is only used for runtime = 'pi'.")
    normalized = normalize_role(role)
    if normalized in _BLOCKED_PI_ROLES:
        raise AgentRuntimeError(
            "hyperresearch-browser-fetcher requires the Claude browser lane and cannot run via Pi."
        )

    agent_path = locate_agent_markdown(vault_root, normalized)
    meta, body = _split_frontmatter(agent_path.read_text(encoding="utf-8"))
    if not body.strip():
        raise AgentRuntimeError(f"Agent prompt is empty: {agent_path}")
    tools = _tool_names(meta)
    model_ref = f"{settings.provider.strip()}/{settings.model.strip()}"
    system_prompt = pi_runtime_preamble(normalized, meta.get("tools")) + "\n---\n\n" + body.lstrip()
    system_prompt_path.write_text(system_prompt, encoding="utf-8")

    args = [
        resolve_pi_command(settings),
        "--no-session",
        "--no-approve",
        "--system-prompt",
        str(system_prompt_path),
        "--model",
        model_ref,
        "--thinking",
        settings.effort.strip() or "low",
    ]
    if tools:
        args += ["--tools", ",".join(tools)]
    else:
        args.append("--no-tools")
    args.append("-p")
    return args, model_ref


def _safe_failure(stderr: str) -> str:
    text = stderr.strip()
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s]+", r"\1<redacted>", text)
    return text[-2000:] if text else "Pi agent process failed."


def run_pi_agent(
    *,
    vault_root: Path,
    role: str,
    prompt: str,
    settings: AgentSettings,
) -> AgentRunResult:
    """Run one Pi-backed role exactly once; never retry a failed model request."""
    with tempfile.TemporaryDirectory(prefix="hyperresearch-pi-") as temp_dir:
        system_prompt_path = Path(temp_dir) / "system-prompt.md"
        args, _model_ref = build_pi_command(
            vault_root=vault_root,
            role=role,
            settings=settings,
            system_prompt_path=system_prompt_path,
        )
        try:
            completed = subprocess.run(
                args,
                cwd=vault_root,
                input=prompt,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentRuntimeError(
                f"Pi agent timed out after {settings.timeout_s}s; request was not retried."
            ) from exc
        except OSError as exc:
            raise AgentRuntimeError(f"Could not launch Pi: {exc}") from exc

    if completed.returncode != 0:
        raise AgentRuntimeError(
            f"Pi agent exited {completed.returncode}; request was not retried. "
            f"{_safe_failure(completed.stderr)}"
        )
    response = completed.stdout.strip()
    if not response:
        raise AgentRuntimeError("Pi agent returned no visible response.")
    return AgentRunResult(
        role=normalize_role(role),
        runtime="pi",
        provider=settings.provider,
        model=settings.model,
        effort=settings.effort,
        response=response,
    )


def read_batch_tasks(path: Path) -> list[dict[str, str]]:
    """Read a bounded list of role/prompt_file tasks for parallel launch."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentRuntimeError(f"Could not read agent batch file: {path}") from exc
    if not isinstance(data, list) or not 1 <= len(data) <= 16:
        raise AgentRuntimeError("Agent batch must contain 1-16 tasks.")
    tasks: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise AgentRuntimeError("Every agent batch task must be an object.")
        role = item.get("role")
        prompt_file = item.get("prompt_file")
        if not isinstance(role, str) or not isinstance(prompt_file, str):
            raise AgentRuntimeError("Every task needs string role and prompt_file fields.")
        tasks.append({"role": role, "prompt_file": prompt_file})
    return tasks
