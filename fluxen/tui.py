"""Textual interface: chat, file tree, workspace agents dashboard, approvals and commands."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, DirectoryTree, Footer, Header, Input, RichLog, Static

from .config import PERMISSIONS, Settings, init_config
from .engine import Agent
from .runs import ACTIVE, Runs
from .session import Sessions
from .tools import Workspace

if TYPE_CHECKING:
    from textual.worker import Worker

    from .codex import CodexAgent

CSS = """
Screen {layout: vertical;}
#body {height: 1fr;}
#sidebar {width: 32%; min-width: 28;}
#sidebar.hidden {display: none;}
#left {height: 1fr; border: round $primary; border-title-color: $primary;}
#agents {height: 14; border: round $secondary; padding: 0 1; overflow-y: auto;}
#right {width: 1fr;}
#chat {height: 1fr; border: round $secondary; padding: 0 1; border-title-color: $secondary;}
#statusbar {height: 1; padding: 0 1;}
#activity {width: 1fr; color: $text-muted;}
#activity.busy {color: $warning;}
#info {width: auto; color: $text-muted;}
#prompt {dock: bottom; margin: 0 1 1 1;}
#prompt.busy {border: tall $warning;}
Approval {align: center middle; background: $background 70%;}
#dialog {
    width: 80%; max-width: 110; height: auto; max-height: 85%;
    border: thick $warning; background: $surface; padding: 1 2;
}
#question {text-style: bold; margin-bottom: 1;}
#details {height: auto; max-height: 24; overflow-y: auto; background: $panel; padding: 0 1;}
#hint {color: $text-muted; margin-top: 1;}
#buttons {height: 3; align-horizontal: right;}
#buttons Button {margin-left: 2;}
"""

COMMANDS = [
    ("/model NAME", "Switch model (no name lists them)"),
    ("/models", "Show configured models"),
    ("/permission MODE", "ask · read-only · auto-edit"),
    ("/plan", "Toggle read-only planning mode"),
    ("/diff", "Show the last file change"),
    ("/undo", "Revert the last file change"),
    ("/sessions", "List saved sessions"),
    ("/resume ID", "Resume a session (a unique prefix is enough)"),
    ("/new", "Start a fresh session"),
    ("/agents", "Show the workspace agents dashboard"),
    ("/clear", "Clear the chat view"),
    ("/config", "Show the config file path"),
    ("/help", "Show commands and keys"),
]
KEYS = [
    ("Enter", "Send prompt"),
    ("Up / Down", "Prompt history"),
    ("Right", "Accept a /command suggestion"),
    ("Esc / Ctrl+C", "Cancel the running task"),
    ("Ctrl+B", "Show or hide the sidebar"),
    ("Ctrl+L", "Clear the chat view"),
    ("Ctrl+T", "Toggle light/dark theme"),
    ("Ctrl+Q", "Quit"),
]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Tool results shown in chat are shortened; the model still receives the full output.
TOOL_PREVIEW_LINES = 12


def diff_line(line: str) -> Text:
    """Colour a unified-diff line; any other text is returned plain."""
    if line.startswith(("+++", "---")):
        return Text(line, style="bold")
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    return Text(line)


def colored(text: str) -> Text:
    return Text("\n").join(diff_line(line) for line in text.split("\n"))


def clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class Approval(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "decide(True)", "Approve"),
        Binding("n,escape", "decide(False)", "Reject"),
    ]
    VERBS = {
        "run_shell": "run this command",
        "write_file": "write this file",
        "edit_file": "apply this edit",
        "delete_file": "delete this file",
    }

    def __init__(self, message: str):
        super().__init__()
        self.tool, _, self.details = message.partition("\n")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            verb = self.VERBS.get(self.tool, "run " + self.tool)
            yield Static(Text(f"Allow the agent to {verb}?"), id="question")
            # Plain Text: diffs and commands often contain [brackets] that are not Rich markup.
            yield Static(colored(self.details or "(no details)"), id="details", markup=False)
            yield Static("y approve · n / Esc reject", id="hint")
            with Horizontal(id="buttons"):
                yield Button("Reject (n)", id="no", variant="error")
                yield Button("Approve (y)", id="yes", variant="success")

    def on_mount(self) -> None:
        # Focus Reject so a stray Enter never approves.
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_decide(self, allowed: bool) -> None:
        self.dismiss(allowed)


class Prompt(Input):
    """Input with shell-style history on Up/Down."""

    BINDINGS = [
        Binding("up", "history(-1)", "Previous prompt", show=False),
        Binding("down", "history(1)", "Next prompt", show=False),
    ]

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.history: list[str] = []
        self.position = 0
        self.draft = ""

    def remember(self, text: str) -> None:
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        self.position = len(self.history)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        if self.position == len(self.history):
            self.draft = self.value
        self.position = max(0, min(len(self.history), self.position + step))
        self.value = self.history[self.position] if self.position < len(self.history) else self.draft
        self.cursor_position = len(self.value)


class Fluxen(App):
    TITLE = "Fluxen · terminal coding agent"
    CSS = CSS
    BINDINGS = [
        # Priority, because the focused prompt binds Ctrl+C to copy; it still copies when idle.
        Binding("escape,ctrl+c", "cancel_task", "Cancel", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+l", "clear_chat", "Clear"),
        Binding("ctrl+t", "toggle_dark", "Theme"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, root: Path, settings: Settings, model: str | None = None, resume=False, backend="litellm"):
        super().__init__()
        self.workspace = Workspace(root, settings.max_output_chars, settings.shell_timeout)
        self.settings = settings
        self.sessions = Sessions(root)
        if resume:
            # resume=True picks the latest session; a string resumes that session ID.
            found = self.sessions.resume(resume if isinstance(resume, str) else None)
            if not found and isinstance(resume, str):
                raise ValueError(f"No unique saved session matches '{resume}'")
        self.backend = backend
        self.agent: Agent | CodexAgent
        if backend == "codex":
            from .codex import CodexAgent

            self.agent = CodexAgent(model, settings, self.workspace, self.sessions, self.emit, self.approve)
        else:
            self.agent = Agent(settings.model(model), settings, self.workspace, self.sessions, self.emit, self.approve)
        self.runs = Runs(self.sessions.dir)
        self.active: Worker[Any] | None = None
        # The worker still counts as running during its own cleanup; this flag ends with the task.
        self.finished = True
        self.partial = ""
        self.replying = False
        self.activity = "Ready"
        self.usage = ""
        self.started = 0.0
        self.frame = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield DirectoryTree(str(self.workspace.root), id="left")
                yield Static(id="agents", markup=False)
            with Vertical(id="right"):
                yield RichLog(id="chat", wrap=True, markup=False, highlight=False, auto_scroll=True)
                with Horizontal(id="statusbar"):
                    yield Static("", id="activity", markup=False)
                    yield Static("", id="info", markup=False)
        yield Prompt(
            placeholder="Describe a task, or type / for commands",
            id="prompt",
            suggester=SuggestFromList(self.suggestions(), case_sensitive=False),
        )
        yield Footer()

    def suggestions(self) -> list[str]:
        names = [command.split()[0] for command, _ in COMMANDS]
        names += [f"/model {m.name}" for m in self.settings.models]
        names += [f"/permission {p}" for p in PERMISSIONS]
        return names

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self.query_one("#left", DirectoryTree).border_title = "Files"
        self.query_one("#chat", RichLog).border_title = "Chat"
        self.refresh_agents()
        self.set_interval(2, self.refresh_agents)
        self.set_interval(0.1, self.tick)
        self.update_info()
        self.set_status("Ready")
        self.say(f"[bold green]Fluxen[/] · {escape(str(self.workspace.root))}")
        if self.sessions.messages:
            self.say(f"[dim]Resumed session {self.sessions.id} ({len(self.sessions.messages)} messages)[/]")
        else:
            self.say(
                "[dim]Describe a task and press Enter. Type [/][bold]/[/][dim] for commands, [/][bold]/help[/]"
                "[dim] for keys, or click a file to insert its path.[/]"
            )

    async def on_unmount(self) -> None:
        close = getattr(self.agent, "close", None)
        if close is not None:
            await close()

    # ---- output ------------------------------------------------------------------------
    def say(self, markup: str):
        """Write Fluxen's own Rich markup. Never pass model, file or tool text here."""
        self.query_one("#chat", RichLog).write(Text.from_markup(markup))

    def write_plain(self, text: str, style: str = ""):
        """Write untrusted text exactly as-is; [brackets] are not interpreted."""
        self.query_one("#chat", RichLog).write(Text(text, style=style))

    def write_labeled(self, label: str, text: str, style: str):
        line = Text()
        line.append(label + " ", style=style)
        line.append(text)
        self.query_one("#chat", RichLog).write(line)

    def write_tool(self, value: str):
        head, _, body = value.partition("\n")
        if head.startswith("Approval required:"):
            # The approval dialog shows the details; keep the chat to one line.
            self.write_labeled("?", head, "bold yellow")
            self.set_status(head)
            return
        name, sep, rest = head.partition(": ")
        line = Text()
        line.append("⚙ ", style="bold yellow")
        if sep:
            line.append(name, style="bold")
            line.append(": " + rest)
        else:
            line.append(head)
        chat = self.query_one("#chat", RichLog)
        chat.write(line)
        if body:
            lines = body.split("\n")
            shown = lines[:TOOL_PREVIEW_LINES]
            chat.write(colored("\n".join("  " + x for x in shown)))
            if len(lines) > len(shown):
                chat.write(Text(f"  … {len(lines) - len(shown)} more lines", style="dim"))

    def flush(self):
        if self.partial:
            self.write_plain(self.partial)
            self.partial = ""
        self.replying = False

    def set_status(self, text: str):
        self.activity = text
        self.render_activity()

    def render_activity(self):
        widget = self.query_one("#activity", Static)
        if self.busy:
            spinner = SPINNER[self.frame % len(SPINNER)]
            elapsed = clock(time.monotonic() - self.started)
            widget.update(Text(f"{spinner} {self.activity} · {elapsed} · Esc to cancel"))
        else:
            widget.update(Text(self.activity))
        widget.set_class(self.busy, "busy")

    def tick(self):
        if self.busy:
            self.frame += 1
            self.render_activity()

    def update_info(self):
        parts = [self.agent.model.name, self.settings.permission]
        if self.agent.plan:
            parts.append("PLAN")
        if self.usage:
            parts.append(self.usage)
        self.query_one("#info", Static).update(Text(" · ".join(parts)))

    def refresh_agents(self):
        if self.runs.current and self.runs.current["status"] in ACTIVE:
            self.runs.save()
        table = Table(title="Workspace agents", expand=True)
        for column in ("Agent / task", "State", "Progress"):
            table.add_column(column, overflow="fold")
        records = self.runs.list()
        for run in records:
            table.add_row(
                Text(run["model"] + " · " + run["task"][:80]), Text(run["status"]), Text(run["progress"][:80])
            )
        if not records:
            table.add_row("No agent runs yet", "Idle", "Enter a task to start")
        self.query_one("#agents", Static).update(table)

    async def emit(self, kind: str, value: str):
        if kind == "token":
            if not self.replying:
                self.say("[bold green]Fluxen:[/]")
                self.replying = True
            self.partial += value
            # Write only whole lines so words are never split across chat entries.
            if "\n" in self.partial:
                done, self.partial = self.partial.rsplit("\n", 1)
                self.write_plain(done)
            self.set_status("Writing reply")
            return
        if kind == "status":
            self.runs.update(progress=value)
            if value == "Max agent steps reached":
                self.runs.update(status="Step limit")
        elif kind == "error":
            self.runs.update(status="Failed", progress=value)
        elif kind == "tool":
            self.runs.update(progress=value.split("\n", 1)[0])
        self.refresh_agents()
        self.flush()
        if kind == "usage":
            self.usage = value
            self.update_info()
        elif kind == "status":
            self.set_status(value)
        elif kind == "error":
            self.write_labeled("✗ Error", value, "bold red")
        else:
            self.write_tool(value)

    async def approve(self, description: str) -> bool:
        self.runs.update(status="Waiting approval", progress=description.split("\n", 1)[0])
        self.refresh_agents()
        self.bell()
        try:
            # Textual types push_screen_wait as Screen[object]; Approval returns bool.
            return bool(await self.push_screen_wait(Approval(description)))  # type: ignore[arg-type]
        finally:
            self.runs.update(status="Running")

    # ---- input ---------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool(self.active and self.active.is_running) and not self.finished

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.query_one("#prompt", Prompt).remember(text)
        if text.startswith("/"):
            self.command(text)
            return
        if self.busy:
            self.say("[yellow]A task is already running; press Esc to cancel it[/]")
            return
        self.say("")
        self.write_labeled("You:", text, "bold cyan")
        self.finished = False
        self.active = self.run_agent(text)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        prompt = self.query_one("#prompt", Input)
        path = Path(event.path).resolve().relative_to(self.workspace.root).as_posix()
        space = "" if not prompt.value or prompt.value.endswith(" ") else " "
        prompt.insert_text_at_cursor(f"{space}{path} ")
        prompt.focus()

    @work(exclusive=True, group="agent")
    async def run_agent(self, text: str):
        self.finished = False
        self.started = time.monotonic()
        self.query_one("#prompt", Input).add_class("busy")
        self.set_status("Thinking")
        self.runs.start(text, self.agent.model.name)
        self.refresh_agents()
        outcome = "Done"
        try:
            await self.agent.run(text)
            if self.runs.current and self.runs.current["status"] in ACTIVE:
                self.runs.update(status="Completed", progress="Done")
        except asyncio.CancelledError:
            self.runs.update(status="Cancelled", progress="Cancelled by user")
            self.flush()
            self.say("[yellow]Task cancelled[/]")
            outcome = "Cancelled"
        except Exception as exc:
            self.runs.update(status="Failed", progress=str(exc))
            self.flush()
            self.write_labeled("✗ Task failed:", str(exc), "bold red")
            outcome = "Failed"
        finally:
            self.finished = True
            self.refresh_agents()
            self.flush()
            self.query_one("#prompt", Input).remove_class("busy")
            self.set_status(f"{outcome} in {clock(time.monotonic() - self.started)} · Ready")
            self.query_one("#left", DirectoryTree).reload()

    def command(self, cmd: str):
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        session_changing = name in ("/resume", "/new")
        if session_changing and self.busy:
            self.say("[yellow]Wait for the running task to finish or press Esc to cancel it[/]")
            return
        if name in ("/model", "/provider"):
            if not arg:
                self.write_plain("Models: " + ", ".join(m.name for m in self.settings.models))
                return
            try:
                if self.backend == "codex":
                    from types import SimpleNamespace

                    self.agent.model = SimpleNamespace(name=arg, model=arg)
                else:
                    self.agent.model = self.settings.model(arg)
                self.write_plain(f"Model: {arg}")
                self.update_info()
            except ValueError as exc:
                self.write_plain(str(exc), style="yellow")
        elif name == "/models":
            for m in self.settings.models:
                current = "  (current)" if m.name == self.agent.model.name else ""
                self.write_plain(f"{m.name}: {m.model} [{m.provider}]{current}")
        elif name == "/permission":
            if arg not in PERMISSIONS:
                self.write_plain(f"Permission: {self.settings.permission}. Choose one of: {', '.join(PERMISSIONS)}")
            else:
                self.settings.permission = arg
                self.write_plain(f"Permission: {arg}")
                self.update_info()
        elif name == "/agents":
            self.query_one("#sidebar").remove_class("hidden")
            self.refresh_agents()
            self.query_one("#agents", Static).scroll_visible()
        elif name == "/plan":
            self.agent.plan = not self.agent.plan
            self.write_plain("Plan mode: " + ("on (the agent will only read and plan)" if self.agent.plan else "off"))
            self.update_info()
        elif name == "/diff":
            self.query_one("#chat", RichLog).write(colored(self.workspace.last_diff or "No agent file diff available"))
        elif name == "/undo":
            self.write_plain(self.workspace.undo_last())
            self.query_one("#left", DirectoryTree).reload()
        elif name == "/sessions":
            rows = self.sessions.list()
            if not rows:
                self.write_plain("No saved sessions for this workspace")
            for row in rows:
                when = row["updated"]
                try:
                    when = datetime.fromisoformat(when).astimezone().strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
                current = " (current)" if row["id"] == self.sessions.id else ""
                self.write_plain(f"{row['id']}  {when}  {row['messages']:>3} msgs  {row['preview']}{current}")
            if rows:
                self.write_plain("Use /resume ID (a unique prefix is enough)", style="dim")
        elif name == "/resume":
            if not arg:
                self.write_plain("Usage: /resume ID (see /sessions)")
            elif self.sessions.resume(arg):
                self._session_switched(f"Resumed session {self.sessions.id} ({len(self.sessions.messages)} messages)")
            else:
                self.write_plain(f"No unique session matches '{arg}'", style="yellow")
        elif name == "/new":
            self.sessions.new()
            self.usage = ""
            self.update_info()
            self._session_switched(f"Started new session {self.sessions.id}")
        elif name == "/clear":
            self.action_clear_chat()
        elif name == "/config":
            self.write_plain(f"Edit {init_config()} then restart for configuration changes")
        elif name == "/help":
            self.show_help()
        else:
            close = [c.split()[0] for c, _ in COMMANDS if c.startswith(name[:3])]
            hint = f" Did you mean {', '.join(close)}?" if close and len(name) > 1 else ""
            self.write_plain(f"Unknown command {name}.{hint} Type /help for all commands.", style="yellow")

    def show_help(self):
        chat = self.query_one("#chat", RichLog)
        for title, rows in (("Commands", COMMANDS), ("Keys", KEYS)):
            table = Table(title=title, title_justify="left", show_header=False, box=None, padding=(0, 2))
            table.add_column(style="bold cyan", no_wrap=True)
            table.add_column()
            for key, text in rows:
                table.add_row(key, text)
            chat.write(table)

    def _session_switched(self, message: str):
        load = getattr(self.agent, "load_thread", None)
        if load is not None:
            load()
        self.write_plain(message)

    def action_cancel_task(self):
        if not self.busy or self.active is None or isinstance(self.screen, Approval):
            # Let the prompt copy (Ctrl+C) or the approval dialog reject (Esc) as usual.
            raise SkipAction()
        self.set_status("Cancelling")
        self.active.cancel()

    def action_toggle_sidebar(self):
        self.query_one("#sidebar").toggle_class("hidden")

    def action_clear_chat(self):
        self.query_one("#chat", RichLog).clear()

    def action_toggle_dark(self):
        self.theme = "textual-light" if self.theme != "textual-light" else "textual-dark"
