# Fluxen — multi-model terminal coding agent

[![CI](https://github.com/majdslmt/Fluxen/actions/workflows/ci.yml/badge.svg)](https://github.com/majdslmt/Fluxen/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

A functional, Python/Textual coding agent for OpenAI, Anthropic, Azure OpenAI, Gemini, OpenRouter, Ollama, Azure AI Foundry and OpenAI-compatible APIs through LiteLLM, plus an optional Codex backend. Every file write, edit, delete and shell command goes through an approval dialog unless you choose otherwise. MIT licensed.

## Quick start

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Git (for Git tools).

```sh
uv tool install git+https://github.com/majdslmt/Fluxen.git
fluxen --init                         # creates the config file and prints its path
export ANTHROPIC_API_KEY='sk-ant-...' # PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-..."
cd /path/to/your/project
fluxen --model claude
```

Upgrade later with `uv tool upgrade fluxen`.

## Install from source

Clone the repository, then from the project directory:

```sh
git clone https://github.com/majdslmt/Fluxen.git
cd Fluxen
uv sync --extra dev
uv run fluxen --init
```

`uv sync` creates and manages `.venv` automatically. You do **not** need to activate a virtual environment or install dependencies with pip. To use Fluxen on other projects, see [Running in other folders and on other machines](#running-in-other-folders-and-on-other-machines).

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
uv run fluxen --model azure
uv run fluxen --continue
```

For Ollama, run `ollama serve` and pull your chosen model; select `/model ollama` and update model name if necessary. For a custom OpenAI-compatible API, configure `model: openai/<your-model>` and `api_base` (ending in `/v1`), plus `CUSTOM_API_KEY` when required. Azure AI Foundry deployments vary by endpoint and authentication; update the `foundry` model, endpoint, and authentication settings according to the selected LiteLLM backend.

### Configuration

Top-level keys in `config.yaml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `default_model` | `gpt` | Model alias used when `--model` is not given |
| `permission` | `ask` | `ask`, `read-only` or `auto-edit` (see Security below) |
| `max_steps` | `12` | Model/tool round trips per task (1–40) |
| `max_output_chars` | `12000` | Cap on a single tool result |
| `shell_timeout` | `120` | Seconds before an approved shell command and everything it started is stopped |

Each entry under `models` takes `name`, `model`, `provider`, and optionally `api_key_env`, `api_base`, `api_version`, `timeout` (seconds per request) and `context_tokens`. Leave `context_tokens` out to use LiteLLM's published input limit for the model (32,768 when the model is unknown, for example a custom deployment). Configs created by older versions set `context_tokens: 32768` on every model; delete that line to use the model's full window. Unknown keys, a `default_model` that isn't defined, and invalid YAML are reported with the file path instead of a traceback.

### Command line

```
fluxen [--model ALIAS] [--permission ask|read-only|auto-edit] [--workspace DIR]
       [--continue | --resume ID] [--backend litellm|codex] [-p PROMPT] [--init] [--version]
```

- `-m/--model ALIAS` selects a configured model (`--provider` still works as an alias).
- `--permission` overrides the configured mode for this run.
- `--continue` resumes the latest session for the workspace; `--resume ID` resumes a specific one (a unique prefix of the ID is enough; see `/sessions`).
- `-p/--prompt TEXT` runs one task without the TUI. The reply goes to stdout and tool activity to stderr. Approvals are asked on the terminal, and are denied automatically when stdin is not a terminal. The exit code is non-zero if the model request failed.

## UI

The left panel shows the file tree; the right panel streams chat and tool activity. Type a natural-language request and press Enter. The workspace agents dashboard below the file tree shows the latest 20 runs across Fluxen instances in the same workspace, including model, task, status, and progress. It refreshes every two seconds; runs without a heartbeat for 15 seconds are marked interrupted. Use `/agents` to bring it into view.

Slash commands:

| Command | Effect |
| --- | --- |
| `/model NAME`, `/models` | Switch model, list configured models |
| `/permission MODE` | Switch permission mode for this run |
| `/plan` | Toggle read-only planning mode |
| `/diff`, `/undo` | Show the last agent file diff, undo the last agent file change |
| `/sessions` | List recent sessions, newest first, with a preview of each |
| `/resume ID`, `/new` | Switch to a saved session, or start a fresh one |
| `/agents`, `/clear`, `/config`, `/help` | Show dashboard, clear chat, config path, commands and keys |

Esc or Ctrl+C cancels a running task (Ctrl+C copies when idle), Ctrl+B hides the sidebar, Ctrl+L clears the chat, Ctrl+T changes theme, Ctrl+Q exits. Up/Down recall earlier prompts, Right accepts a `/command` suggestion, and clicking a file in the tree inserts its path. Approval dialogs show a coloured diff; press `y` to approve or `n`/Esc to reject (Reject is focused, so Enter never approves by accident). The status bar shows live activity and elapsed time on the left; model, permission mode, plan mode, token usage and estimated cost on the right.

## Architecture

```
fluxen/
  cli.py       # CLI entry point and one-shot (-p) mode
  tui.py       # Textual widgets, modal approvals, commands
  config.py    # YAML model profiles, validation, environment credentials
  provider.py  # Unified LiteLLM streaming/tool-call accumulation
  engine.py    # Agent step loop, context budget, approval policy, cost
  tools.py     # Workspace files, search, edits, diff, Git, shell, undo
  session.py   # Atomic local JSON session persistence
  runs.py      # Workspace agents dashboard records shared between instances
  codex.py     # Codex app-server backend
```

The agent streams assistant text and buffers tool calls from provider deltas. Tools execute only once their complete arguments are received. Tool results are sent back to the model for subsequent steps. The repository content is treated as untrusted input in the system instructions.

Tools available to the model: `read_file` (paged with `offset`/`limit`, and says when output is truncated), `list_files`, `search`, `edit_file` (exact-string replacement that must match once unless `replace_all` is set; preserves the file's line endings), `write_file`, `delete_file`, `run_shell`, `git_status` and `git_diff`.

Context is budgeted in tokens using LiteLLM's tokenizer, with 15% of the window kept free for the reply. The current request is always sent; long tool results are shortened (keeping their start and end) before older steps are dropped, and an assistant tool call is never separated from its results. If a task is cancelled mid-step, unanswered tool calls are recorded as cancelled so the session stays valid for the next request and for `--continue`.

## Example task

```sh
mkdir demo && cd demo
git init
printf 'def add(a,b):\n    return a-b\n' > calc.py
uv run --project /path/to/fluxen fluxen --workspace . --model gpt
```

Prompt: `Fix the bug in calc.py, add a unit test, and run it.` The model reads files, proposes edits, and asks permission for writes and shell commands. Approve requested actions individually. Review `/diff`, Git status, and the generated test output.

## Running in other folders and on other machines

Fluxen works on one folder, the *workspace*: the current directory, or the one given with `--workspace`. Sessions, `--continue` and `/sessions` are kept per workspace, and every Fluxen window open on the same workspace shares the workspace agents dashboard.

### Another folder on the same machine

Install the `fluxen` command once, then start it from any project:

```sh
uv tool install --editable .     # from the Fluxen checkout; code changes apply immediately
cd /path/to/other/project
fluxen --model claude
```

If `fluxen` is not found afterwards, run `uv tool update-shell` and open a new terminal. Without installing, point `uv` at the checkout instead:

```sh
uv run --project /path/to/Fluxen fluxen --workspace /path/to/other/project --model claude
```

### Another machine

Fluxen is a terminal app, so either install it on that machine, or install it there once and use it over SSH.

**Install on the machine.** It needs `uv` and Git, plus its own API key or `codex login`; configuration and sessions are stored per machine.

```sh
uv tool install git+https://github.com/majdslmt/Fluxen.git
fluxen --init
export ANTHROPIC_API_KEY='sk-ant-...'
cd /path/to/project && fluxen --model claude
```

**Use it over SSH.** After installing on the remote machine as above, connect and run it there. The full interface works over SSH; on Windows use Windows Terminal for colours and key bindings. To keep a long task running after you disconnect, start Fluxen inside `tmux` (or `screen`) and reattach later with `tmux attach`.

```sh
ssh user@server
cd /path/to/project
tmux new -s fluxen
fluxen --model claude
```

### Scripts, scheduled tasks and CI

`-p` runs one task without the interface and prints the reply:

```sh
fluxen --workspace /path/to/project --model claude --permission auto-edit -p "Fix the failing test in calc.py"
```

With `--permission auto-edit` file changes are applied without asking. Shell commands still need approval: they are asked on the terminal, and refused automatically when nothing is attached to stdin (cron, CI, scheduled tasks). Run unattended tasks only on a Git checkout or a disposable copy, and review the result with `git diff`.

## Development

```sh
uv run pytest -q
uv run ruff format fluxen tests && uv run ruff check fluxen tests
uv run mypy fluxen
```

GitHub Actions runs the same checks on Linux, macOS and Windows with Python 3.11–3.13.

Contributions are welcome: open an issue to discuss larger changes first, and make sure the checks above pass before sending a pull request.

## Security boundaries and current limitations

- `ask` (default): prompt for every write/edit/delete/shell command; `read-only`: refuse all of them; `auto-edit`: automatic file writes/edits/deletes but still ask for shell commands.
- Writes, edits and deletes under any `.git` directory are always refused, because Git hooks and config can run commands.
- File operations reject paths resolving outside the workspace and avoid symlink traversal in file indexing and search. These are **path guards, not OS-level confinement**; concurrent symlink modification remains a risk.
- **Approved shell commands are not sandboxed**: they can read/write outside the workspace and access the network/credentials using the user's OS privileges. Run in a container or restricted OS account when handling untrusted repositories. Approval does not make a command safe. Commands run in their own process group, and on timeout or cancel the whole group is stopped; a process that deliberately detaches into a new session can still outlive it.
- User approval applies to agent-triggered tools, not manual `/undo`. Session files can contain source code and tool results; protect local user data and avoid submitting secrets to third-party models.
- Sophisticated repository indexing and prompt injection defenses are not yet implemented.
- Costs are estimates from LiteLLM's price table and are hidden for models it doesn't know. Rich diff views, reusable persistent checkpoints, an MCP client/server, resumable interrupted tool calls, and plugin discovery are roadmap items.
- Provider compatibility and tool calling depend on model/deployment capabilities. The default model profiles are configurable, not a guarantee each endpoint currently exists. No live-provider tests run without user credentials.

This is a working foundation, **not yet fully production hardened or feature-complete** relative to mature coding agents. Do not enable unrestricted automatic shell execution.

## Codex backend

Install the [Codex CLI](https://github.com/openai/codex) (for example `npm install -g @openai/codex`) and sign in, then launch Fluxen. No API key is needed; Codex uses your own login.

```powershell
codex login
uv run fluxen --backend codex
```

Codex runs through its local app-server using your Codex authentication and default model. The app-server is started once and reused for later turns; it is restarted after a cancelled or failed turn. Pass `--model MODEL_ID` to select a Codex model. Use `--continue` or `--resume ID` to resume the saved Codex thread. Streamed replies and tool activity appear in chat and the workspace agents dashboard. Approval requests use the Fluxen approval dialog. Codex enforces its sandbox and approval policy; its policy differs from the LiteLLM backend's per-tool approval policy. `/plan` uses a read-only sandbox. `/diff` shows the latest Codex turn diff; `/undo` applies only to edits made by Fluxen tools, not Codex edits.

Protocol reference: [Codex App Server](https://learn.chatgpt.com/docs/app-server).

## License

MIT. See [LICENSE](LICENSE).
