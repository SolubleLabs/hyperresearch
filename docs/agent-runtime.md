# Agent runtime and model-provider switching

Hyperresearch has one research pipeline. The model-agent runtime is a replaceable execution layer for the named subagents that the pipeline already uses.

## Configuration

The project vault stores only non-secret routing information:

```toml
[agents]
runtime = "native"
provider = ""
model = ""
effort = "low"
command = "pi"
timeout_s = 900
```

- `runtime = "native"` keeps the existing host behavior: Claude Code spawns Task agents and Codex spawns custom agents.
- `runtime = "pi"` runs the same installed Claude-format Hyperresearch agent prompt through Pi.
- `provider` and `model` are Pi identifiers. They are deliberately independent from the pipeline profile/gear.
- `effort` is the Pi thinking level.
- `command` is the Pi executable or an absolute path to it.
- `timeout_s` is one leaf-agent invocation timeout.

Hyperresearch does not store provider credentials. A Pi provider extension/config owns authentication.

## Switching

Configure a Pi backend once:

```bash
hyperresearch agent use pi \
  --provider tokenme \
  --model google/gemma-4-31b-it \
  --effort low \
  --command /absolute/path/to/pi
```

Switch back to the current native host:

```bash
hyperresearch agent use native
```

Switch back to the previously configured Pi provider/model without repeating them:

```bash
hyperresearch agent use pi
```

`agent use` changes only `[agents]`. It does not create, rename, reset, or edit a run manifest, `vault_tag`, notes, fetched sources, reports, or step state.

## What the installed skills do

Every generated Hyperresearch entry skill and step procedure receives a small runtime-dispatch contract.

Before a subagent spawn it reads:

```bash
hyperresearch agent show -j
```

Then:

- `native`: use the step's existing native Codex/Claude spawn mechanism.
- `pi`: materialize the exact spawn prompt the step already specifies and call:
  `hyperresearch agent run <role> --prompt-file <path> -j`.
- Parallel Pi waves use a JSON task list with `hyperresearch agent batch <tasks.json> -j`.

The role prompt itself is not forked. Pi reuses the installed Claude-format `.claude/agents/hyperresearch-<role>.md` (or the global equivalent), strips its frontmatter, maps its declared Read/Write/Edit/Bash tools to Pi, and adds a narrow runtime translation for unsupported Task/WebSearch/browser operations.

The browser-fetcher remains native Claude-only. Pi leaves blocked browser escalations for the human.

## Windows prompt transport

Hyperresearch role prompts are often larger than the Windows command-line limit. `agent run` therefore:

1. writes the system/role prompt to an OS temporary UTF-8 file;
2. passes only that short file path through `--system-prompt`;
3. pipes the per-spawn user prompt over stdin;
4. removes the temporary directory when Pi exits.

This also avoids putting source-rich prompts into process command lines.

## Failure and billing rules

`agent run` invokes Pi exactly once and does not retry a failed process. Pi's OpenAI-compatible transport defaults to zero HTTP retries.

Tool-use loops can still make more than one provider request as part of one successful agent conversation; those are model turns, not retry attempts.

Hyperresearch never silently falls back from one provider/model to another after a failed request. Switch explicitly with `agent use` after inspecting the failure. This prevents an upstream timeout/503 from being duplicated into a second paid request.

`agent batch` launches the requested independent roles concurrently and does not retry failed members. If any member fails, the batch reports failure instead of substituting a different model.

## Provider example: TokenMe

TokenMe is an OpenAI-compatible model provider, not an agent runtime. Use it through Pi.

A Pi provider should:

- load TokenMe credentials through TokenMe's own secret/config mechanism;
- register current catalog model IDs under a `tokenme` provider;
- keep the key in process memory only;
- advertise model context, image and reasoning capabilities accurately;
- preserve no-retry behavior for billed requests;
- declare provider compatibility accurately. For TokenMe Chat, set Pi `compat.supportsStore = false`; otherwise Pi emits OpenAI `store: false`, which is outside TokenMe's verified request contract and can make an otherwise healthy model unroutable.

The Hyperresearch repository itself does not need or know `TOKENME_API_KEY`.

## Acceptance

A runtime integration is not accepted just because the provider appears in a model list. Prove all of these:

1. `hyperresearch agent show -j` reports the intended runtime/provider/model.
2. Pi can list the selected provider/model without a generation call.
3. A named Hyperresearch role can perform at least one representative tool call and return a nonempty response.
4. `agent use native` then `agent use pi` changes only `[agents]` and preserves the active run.
5. The project's original native installer tests and generated skill/step tests stay green.
6. No credential is written into `.hyperresearch/config.toml`, skills, prompts, logs, or test artifacts.
