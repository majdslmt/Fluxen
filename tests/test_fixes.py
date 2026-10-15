"""Regression tests for bugs fixed in the improvements pass, and for the new features."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from textual.widgets import RichLog

from fluxen import cli
from fluxen.codex import CodexAgent
from fluxen.config import ConfigError, Model, Settings, load
from fluxen.engine import CANCELLED, Agent, repair
from fluxen.session import Sessions
from fluxen.tools import Workspace
from fluxen.tui import Fluxen


async def noop_emit(kind, value):
    pass


def call(call_id, name, **args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def valid_sequence(messages):
    """Every tool result follows its call and every call is answered."""
    pending = set()
    for m in messages:
        if m["role"] == "tool":
            assert m["tool_call_id"] in pending, "orphaned tool result"
            pending.discard(m["tool_call_id"])
        else:
            assert not pending, "tool call left unanswered"
            if m["role"] == "assistant":
                pending = {c["id"] for c in m.get("tool_calls") or []}
    assert not pending


# ---- shell -------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
async def test_shell_timeout_kills_background_children(tmp_path):
    w = Workspace(tmp_path, shell_timeout=1)
    started = time.monotonic()
    result = await asyncio.wait_for(w.execute("run_shell", {"command": "sleep 100 & sleep 100"}), 20)
    assert "timed out after 1s" in result
    assert time.monotonic() - started < 10


async def test_shell_reports_exit_code(tmp_path):
    w = Workspace(tmp_path)
    assert (await w.execute("run_shell", {"command": "exit 3"})).startswith("exit=3")


# ---- context window ----------------------------------------------------------------------
def make_agent(tmp_path, messages, context_tokens=3000, provider=None):
    settings = Settings(models=[Model("demo", "openai/test", "openai", context_tokens=context_tokens)])
    sessions = Sessions(tmp_path)
    sessions.messages = messages
    return Agent(settings.model("demo"), settings, Workspace(tmp_path), sessions, noop_emit, None, provider)


def test_history_keeps_request_when_tool_result_is_huge(tmp_path):
    messages = [
        {"role": "user", "content": "summarise big.txt"},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "read_file", path="big.txt")]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x " * 40000},
    ]
    history = make_agent(tmp_path, messages)._history()
    assert [m["role"] for m in history] == ["system", "user", "assistant", "tool"]
    assert history[1]["content"] == "summarise big.txt"
    assert "omitted to fit the context window" in history[3]["content"]
    valid_sequence(history[1:])


def test_history_drops_old_turns_whole(tmp_path):
    messages = []
    for i in range(40):
        messages += [
            {"role": "user", "content": f"task {i}"},
            {"role": "assistant", "content": None, "tool_calls": [call(f"c{i}", "git_status")]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "git_status", "content": "M file.py\n" * 30},
            {"role": "assistant", "content": f"done {i}"},
        ]
    history = make_agent(tmp_path, messages)._history()
    assert history[-1]["content"] == "done 39"
    assert len(history) < len(messages)
    assert history[1]["role"] != "tool"
    valid_sequence(history[1:])


def test_repair_answers_calls_and_drops_orphans():
    messages = [
        {"role": "tool", "tool_call_id": "old", "content": "orphan"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [call("a", "git_status"), call("b", "git_diff")]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    fixed = repair(messages)
    valid_sequence(fixed)
    assert fixed[0]["role"] == "user"
    assert {"role": "tool", "tool_call_id": "b", "content": CANCELLED} in fixed


# ---- cancellation ------------------------------------------------------------------------
class WriteProvider:
    async def stream(self, model, messages, tools):
        yield "call", call("w1", "write_file", path="a.txt", content="hi")
        yield "call", call("w2", "write_file", path="b.txt", content="hi")


async def test_cancel_during_approval_leaves_valid_session(tmp_path):
    agent = make_agent(tmp_path, [], context_tokens=100000, provider=WriteProvider())
    waiting = asyncio.Event()

    async def approve(message):
        waiting.set()
        await asyncio.Event().wait()  # user never answers

    agent.approve = approve
    task = asyncio.create_task(agent.run("write two files"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    valid_sequence(agent.sessions.messages)
    saved = json.loads((agent.sessions.dir / f"{agent.sessions.id}.json").read_text())
    valid_sequence(saved["messages"])
    assert saved["messages"][-1]["content"] == CANCELLED


# ---- usage and cost ------------------------------------------------------------------------
class UsageProvider:
    async def stream(self, model, messages, tools):
        yield "text", "hi"
        yield "usage", (1000, 1000)


async def test_usage_includes_estimated_cost(tmp_path):
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    settings = Settings(models=[Model("gpt", "openai/gpt-4.1", "openai")])
    agent = Agent(settings.model(), settings, Workspace(tmp_path), Sessions(tmp_path), emit, None, UsageProvider())
    await agent.run("hello")
    usage = [v for k, v in events if k == "usage"][-1]
    assert "input 1,000" in usage and "~$" in usage


# ---- tools ---------------------------------------------------------------------------------
async def test_edit_file(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\ny = 1\n")
    await w.execute("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert (tmp_path / "a.py").read_text() == "x = 2\ny = 1\n"
    with pytest.raises(ValueError, match="matches 2 times"):
        await w.execute("edit_file", {"path": "a.py", "old_string": "= ", "new_string": "="})
    with pytest.raises(ValueError, match="not found"):
        await w.execute("edit_file", {"path": "a.py", "old_string": "zzz", "new_string": ""})
    await w.execute("edit_file", {"path": "a.py", "old_string": " = ", "new_string": "=", "replace_all": True})
    assert (tmp_path / "a.py").read_text() == "x=2\ny=1\n"
    assert "-x = 2" in w.last_diff
    w.undo_last()
    assert (tmp_path / "a.py").read_text() == "x = 2\ny = 1\n"


async def test_edit_file_preserves_crlf(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    await w.execute("edit_file", {"path": "win.txt", "old_string": "one\ntwo", "new_string": "one\n2"})
    assert (tmp_path / "win.txt").read_bytes() == b"one\r\n2\r\n"


async def test_edit_requires_approval_with_diff(tmp_path):
    (tmp_path / "a.txt").write_text("old\n")
    requests = []

    class EditProvider:
        async def stream(self, model, messages, tools):
            if messages[-1]["role"] == "tool":
                yield "text", "ok"
            else:
                yield "call", call("e1", "edit_file", path="a.txt", old_string="old", new_string="new")

    async def approve(message):
        requests.append(message)
        return True

    agent = make_agent(tmp_path, [], context_tokens=100000, provider=EditProvider())
    agent.approve = approve
    await agent.run("edit")
    assert "+new" in requests[0]
    assert (tmp_path / "a.txt").read_text() == "new\n"


async def test_read_file_pages_and_marks_truncation(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    first = await w.execute("read_file", {"path": "long.txt"})
    assert first.startswith("line 1\n")
    assert "showing lines 1-400 of 1000; use offset=401" in first
    later = await w.execute("read_file", {"path": "long.txt", "offset": 990})
    assert later.startswith("line 990\n") and "of 1000]" in later
    small = Workspace(tmp_path, limit=1000)
    capped = await small.execute("read_file", {"path": "long.txt"})
    assert "use offset=" in capped and len(capped) < 1200


async def test_search_skips_ignored_directories(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("needle")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle = 1\n")
    assert await w.execute("search", {"query": "needle"}) == "src/app.py:1: needle = 1"


# ---- sessions ------------------------------------------------------------------------------
def test_sessions_listed_newest_first_and_resumable_by_prefix(tmp_path):
    with patch("fluxen.session.user_data_dir", return_value=str(tmp_path / "data")):
        ids = []
        for i in range(3):
            s = Sessions(tmp_path)
            s.messages = [{"role": "user", "content": f"task {i}"}]
            s.save()
            ids.append(s.id)
            time.sleep(0.02)
        (s.dir / "broken.json").write_text("{")
        listing = Sessions(tmp_path).list()
        assert [row["id"] for row in listing] == ids[::-1]
        assert listing[0]["preview"] == "task 2"
        other = Sessions(tmp_path)
        assert other.resume(ids[0][:6])
        assert other.messages[0]["content"] == "task 0"
        assert not other.resume("does-not-exist")


# ---- config --------------------------------------------------------------------------------
def test_config_errors_are_readable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  - {name: a, model: openai/x, provider: openai, typo: 1}\n")
    with pytest.raises(ConfigError, match="unknown key.*typo"):
        load(path)
    path.write_text("default_model: nope\nmodels:\n  - {name: a, model: openai/x, provider: openai}\n")
    with pytest.raises(ConfigError, match="default_model 'nope'"):
        load(path)
    path.write_text("models: [\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(path)


def test_context_window_from_model_info():
    assert Model("x", "anthropic/claude-sonnet-4-5-20250929", "anthropic").context_window() >= 200000
    assert Model("x", "openai/unknown-model", "openai").context_window() == 32768
    assert Model("x", "openai/gpt-4.1", "openai", context_tokens=5000).context_window() == 5000


# ---- TUI -----------------------------------------------------------------------------------
def chat_lines(app):
    log = app.query_one("#chat", RichLog)
    return [strip.text.rstrip() for strip in log.lines]


async def test_tui_shows_brackets_and_streams_whole_lines(tmp_path):
    with patch("fluxen.session.user_data_dir", return_value=str(tmp_path / "data")):
        app = Fluxen(tmp_path, Settings())
    async with app.run_test(size=(120, 40)) as pilot:
        await app.emit("tool", "read_file: arr[/] = 1 and [bold]x[/bold] and a[/path]")
        for token in ["Hel", "lo wor", "ld\nsecond ", "line"]:
            await app.emit("token", token)
        app.flush()
        await pilot.pause()
        lines = chat_lines(app)
        assert any("arr[/] = 1 and [bold]x[/bold] and a[/path]" in line for line in lines)
        assert "Hello world" in lines
        assert "second line" in lines


# ---- CLI -----------------------------------------------------------------------------------
def test_cli_version_and_workspace_validation(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.parse(["--version"])
    assert "fluxen" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.parse(["--workspace", str(tmp_path / "missing")])
    assert cli.parse(["--provider", "azure", "--workspace", str(tmp_path)]).model == "azure"


async def test_cli_one_shot_prompt(tmp_path, capsys):
    class Reply:
        async def stream(self, model, messages, tools):
            yield "text", "the answer"

    args = cli.parse(["-p", "question", "--workspace", str(tmp_path), "-m", "demo"])
    settings = Settings(models=[Model("demo", "openai/test", "openai")], default_model="demo")
    with (
        patch("fluxen.session.user_data_dir", return_value=str(tmp_path / "data")),
        patch("fluxen.engine.Provider", Reply),
    ):
        assert await cli.run_once(args, settings) == 0
    assert "the answer" in capsys.readouterr().out


# ---- Codex ---------------------------------------------------------------------------------
def test_codex_thread_follows_session(tmp_path):
    sessions = SimpleNamespace(dir=tmp_path, id="one")
    (tmp_path / "one.codex").write_text("thread-1")
    agent = CodexAgent(None, Settings(), SimpleNamespace(root=tmp_path, last_diff=""), sessions, noop_emit, None)
    assert agent.thread_id == "thread-1"
    sessions.id = "two"
    agent.load_thread()
    assert agent.thread_id is None


FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write("start\n")
turns = 0
for line in sys.stdin:
    msg = json.loads(line)
    method, ident = msg.get("method"), msg.get("id")
    def reply(result):
        print(json.dumps({"id": ident, "result": result}), flush=True)
    def notify(method, params):
        print(json.dumps({"method": method, "params": params}), flush=True)
    if method == "initialize":
        reply({})
    elif method in ("thread/start", "thread/resume"):
        reply({"thread": {"id": msg["params"].get("threadId", "t1")}})
    elif method == "turn/start":
        turns += 1
        reply({"turn": {"id": f"turn{turns}"}})
        notify("item/agentMessage/delta", {"delta": f"reply {turns}"})
        notify("turn/completed", {"turn": {"id": f"turn{turns}", "status": "completed"}})
"""


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX script as a fake codex executable")
async def test_codex_reuses_app_server_between_turns(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(FAKE_CODEX.replace("/usr/bin/env python3", sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir), prepend=":")
    sessions = SimpleNamespace(dir=tmp_path, id="s", messages=[], save=lambda: None)
    workspace = SimpleNamespace(root=tmp_path, last_diff="")
    agent = CodexAgent(None, Settings(), workspace, sessions, noop_emit, None)
    try:
        await agent.run("first")
        await agent.run("second")
    finally:
        await agent.close()
    assert log.read_text().count("start") == 1
    assert [m["content"] for m in sessions.messages] == ["first", "reply 1", "second", "reply 2"]
    assert (tmp_path / "s.codex").read_text() == "t1"
