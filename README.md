# Fluxen — multi-model terminal coding agent

A functional, Python/Textual coding agent for OpenAI, Anthropic, Azure OpenAI, Gemini, OpenRouter, Ollama, Azure AI Foundry and OpenAI-compatible APIs through LiteLLM. MIT licensed.

## Install

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Git (for Git tools). Install `uv` using the official instructions for your platform, then from the project directory:

```sh
uv sync --extra dev
uv run fluxen --init
```

`uv sync` creates and manages `.venv` automatically. You do **not** need to activate a virtual environment or install dependencies with pip. To launch from a different workspace after installing the project, use `uv run --project /path/to/fluxen fluxen --workspace /path/to/repository` (the CLI options are documented below). For a globally available `fluxen` command, run `uv tool install --editable .` from the project directory.

Edit the config path printed by `fluxen --init` (`~/.config/fluxen/config.yaml` on many Unix systems). **Model identifiers are examples** and can be changed to available models/deployments. Store **keys only in environment variables**. For example:

```sh
export OPENAI_API_KEY='...'
export ANTHROPIC_API_KEY='...'
export AZURE_API_KEY='...'
export AZURE_API_BASE='https://YOUR-RESOURCE.openai.azure.com/'
export GEMINI_API_KEY='...'
export OPENROUTER_API_KEY='...'
# In config.yaml set the azure model to "azure/YOUR_DEPLOYMENT_NAME"
# Set the Azure api_base to your resource endpoint, or AZURE_API_BASE in your environment.
uv run fluxen --model gpt
uv run fluxen --model claude
uv run fluxen --provider azure
uv run fluxen --continue
```

For Ollama, run `ollama serve` and pull your chosen model; select `/model ollama` and update model name if necessary. For a custom OpenAI-compatible API, configure `model: openai/<your-model>` and `api_base` (ending in `/v1`), plus `CUSTOM_API_KEY` when required. Azure AI Foundry deployments vary by endpoint and authentication; update the `foundry` model, endpoint, and authentication settings according to the selected LiteLLM backend.

## UI

The left panel shows the file tree; the right panel streams chat and tool activity. Type a natural-language request and press Enter. The workspace agents dashboard below the file tree shows the latest 20 runs across Fluxen instances in the same workspace, including model, task, status, and progress. It refreshes every two seconds; runs without a heartbeat for 15 seconds are marked interrupted. Use `/agents` to bring it into view.

Slash commands: `/model NAME`, `/provider NAME`, `/models`, `/agents`, `/plan`, `/diff`, `/undo`, `/sessions`, `/config`, `/help`. Ctrl+C cancels a running task, Ctrl+L clears view, Ctrl+D changes theme, Ctrl+Q exits. The status bar shows model and reported token usage. `uv run fluxen --continue` restores the latest saved session for this workspace.

## Architecture

```
fluxen/
  cli.py       # CLI entry point
  tui.py       # Textual widgets, modal approvals, commands
  config.py    # YAML model profiles and environment credentials
  provider.py  # Unified LiteLLM streaming/tool-call accumulation
  engine.py    # Agent step loop, context budget, approval policy
  tools.py     # Workspace files, search, diff, Git, shell, undo
  session.py   # Atomic local JSON session persistence
```

The agent streams assistant text and buffers tool calls from provider deltas. Tools execute only once their complete arguments are received. Tool results are sent back to the model for subsequent steps. The repository content is treated as untrusted input in the system instructions.

## Example task

```sh
mkdir demo && cd demo
git init
printf 'def add(a,b):\n    return a-b\n' > calc.py
uv run --project /path/to/fluxen fluxen --workspace . --model gpt
```

Prompt: `Fix the bug in calc.py, add a unit test, and run it.` The model reads files, proposes edits, and asks permission for writes and shell commands. Approve requested actions individually. Review `/diff`, Git status, and the generated test output.

## Tests

```sh
uv run --extra dev pytest -q
uv run python -m compileall -q fluxen
```

## Security boundaries and current limitations

- `ask` (default): prompt for every write/delete/shell command; `read-only`: refuse all three; `auto-edit`: automatic file writes/deletes but still ask for shell commands.
- File operations reject paths resolving outside the workspace and avoid symlink traversal in file indexing. These are **path guards, not OS-level confinement**; concurrent symlink modification remains a risk.
- **Approved shell commands are not sandboxed**: they can read/write outside the workspace and access the network/credentials using the user's OS privileges. Run in a container or restricted OS account when handling untrusted repositories. Approval does not make a command safe.
- User approval applies to agent-triggered tools, not manual `/undo`. Session files can contain source code and tool results; protect local user data and avoid submitting secrets to third-party models.
- Context trimming uses a conservative character-based approximation; exact tokenizer-based budgeting, sophisticated repository indexing, and prompt injection defenses are not yet implemented.
- Only reported token counts are shown; prices vary by provider. Estimated cost is not yet displayed. Rich diff views, reusable persistent checkpoints, an MCP client/server, resumable interrupted tool calls, and plugin discovery are roadmap items.
- Provider compatibility and tool calling depend on model/deployment capabilities. The default model profiles are configurable, not a guarantee each endpoint currently exists. No live-provider tests run without user credentials.

This is a working foundation, **not yet fully production hardened or feature-complete** relative to mature coding agents. Do not enable unrestricted automatic shell execution.

## Codex backend

Install Codex CLI and sign in, then launch Fluxen:

```powershell
codex login
uv run fluxen --backend codex
```

Codex runs through its local app-server using your Codex authentication and default model. Pass `--model MODEL_ID` to select a Codex model. Use `--continue` to resume the saved Codex thread. Streamed replies and tool activity appear in chat and the workspace agents dashboard. Approval requests use the Fluxen approval dialog. Codex enforces its sandbox and approval policy; its policy differs from the LiteLLM backend's per-tool approval policy. `/plan` uses a read-only sandbox. `/diff` shows the latest Codex turn diff; `/undo` applies only to edits made by Fluxen tools, not Codex edits.

Protocol reference: [Codex App Server](https://learn.chatgpt.com/docs/app-server).
