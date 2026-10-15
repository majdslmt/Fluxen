"""TUI interaction: keys, history, approvals, status bar and compact tool output."""

import asyncio
from unittest.mock import patch

from textual.widgets import Input, RichLog, Static

from fluxen.config import Settings
from fluxen.tui import TOOL_PREVIEW_LINES, Approval, Fluxen


def make_app(tmp_path):
    with patch("fluxen.session.user_data_dir", return_value=str(tmp_path / "data")):
        return Fluxen(tmp_path, Settings())


def chat_text(app):
    return "\n".join(strip.text.rstrip() for strip in app.query_one("#chat", RichLog).lines)


def static_text(app, selector):
    return str(app.query_one(selector, Static).render())


async def test_prompt_history_and_help(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press(*"/help", "enter")
        await pilot.press(*"/plan", "enter")
        await pilot.pause()
        assert "Prompt history" in chat_text(app)
        assert "PLAN" in static_text(app, "#info")
        prompt = app.query_one("#prompt", Input)
        await pilot.press("up")
        assert prompt.value == "/plan"
        await pilot.press("up")
        assert prompt.value == "/help"
        await pilot.press("down", "down")
        assert prompt.value == ""


async def test_escape_cancels_running_task(tmp_path):
    app = make_app(tmp_path)

    async def forever(prompt):
        await asyncio.sleep(60)

    app.agent.run = forever
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press(*"go", "enter")
        await pilot.pause()
        assert app.busy
        assert "Esc to cancel" in static_text(app, "#activity")
        await pilot.press("escape")
        await app.active.wait()
        await pilot.pause()
        assert not app.busy
        assert "Task cancelled" in chat_text(app)
        assert "Cancelled in" in static_text(app, "#activity")


async def test_approval_keys_and_escape_rejects_without_cancelling(tmp_path):
    app = make_app(tmp_path)
    answers = []

    async def asks(prompt):
        answers.append(await app.approve("run_shell\necho [hi]"))
        answers.append(await app.approve("write_file\n+new line"))

    app.agent.run = asks
    async with app.run_test(size=(120, 40)) as pilot:
        app.active = app.run_agent("task")
        await pilot.pause()
        assert isinstance(app.screen, Approval)
        await pilot.press("escape")
        await pilot.pause()
        assert app.busy  # Esc rejected the action but did not cancel the task
        await pilot.press("y")
        await app.active.wait()
    assert answers == [False, True]


async def test_long_tool_output_is_compacted(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        body = "\n".join(f"row {i}" for i in range(40))
        await app.emit("tool", "read_file: big.txt\n" + body)
        await app.emit("usage", "input 10 · output 5 tokens")
        await pilot.pause()
        text = chat_text(app)
        assert f"row {TOOL_PREVIEW_LINES - 1}" in text
        assert f"row {TOOL_PREVIEW_LINES}" not in text
        assert f"{40 - TOOL_PREVIEW_LINES} more lines" in text
        assert "output 5 tokens" in static_text(app, "#info")
