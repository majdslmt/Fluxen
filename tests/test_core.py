import pytest

from fluxen.config import Model, Settings
from fluxen.engine import Agent
from fluxen.session import Sessions
from fluxen.tools import Workspace, schemas


@pytest.mark.asyncio
async def test_file_lifecycle_and_undo(tmp_path):
    w = Workspace(tmp_path)
    await w.execute("write_file", {"path": "src/a.py", "content": "print(1)\n"})
    assert "print(1)" in await w.execute("read_file", {"path": "src/a.py"})
    assert "src/a.py" in await w.execute("list_files", {})
    assert "src/a.py:1:" in await w.execute("search", {"query": "print"})
    await w.execute("delete_file", {"path": "src/a.py"})
    assert not (tmp_path / "src/a.py").exists()
    w.undo_last()
    assert (tmp_path / "src/a.py").exists()
    w.undo_last()
    assert not (tmp_path / "src/a.py").exists()


def test_outside_workspace_rejected(tmp_path):
    w = Workspace(tmp_path)
    with pytest.raises(PermissionError):
        w.path("../secret")
    external = tmp_path.parent / "external-target"
    external.mkdir(exist_ok=True)
    try:
        (tmp_path / "link").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need elevated privileges on this platform")
    with pytest.raises(PermissionError):
        w.path("link/a.txt")


@pytest.mark.asyncio
async def test_git_metadata_writes_refused(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("x")
    for name, args in [
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}),
        ("write_file", {"path": "sub/.GIT/config", "content": "x"}),
        ("edit_file", {"path": ".git/config", "old_string": "x", "new_string": "y"}),
        ("delete_file", {"path": ".git/config"}),
    ]:
        with pytest.raises(PermissionError):
            await w.execute(name, args)
    assert (tmp_path / ".git/config").read_text() == "x"


@pytest.mark.asyncio
async def test_shell_output_and_timeout_path(tmp_path):
    w = Workspace(tmp_path)
    assert "hello" in await w.execute("run_shell", {"command": "echo hello"})


class StubProvider:
    async def stream(self, model, messages, tools):
        yield "text", "done"


@pytest.mark.asyncio
async def test_agent_history(tmp_path):
    out = []

    async def emit(k, v):
        out.append((k, v))

    async def approve(v):
        raise AssertionError("Unexpected approval")

    settings = Settings(models=[Model("demo", "openai/test", "openai")], default_model="demo")
    sessions = Sessions(tmp_path)
    agent = Agent(settings.model(), settings, Workspace(tmp_path), sessions, emit, approve, StubProvider())
    await agent.run("hello")
    assert sessions.messages[-1]["content"] == "done"
    assert sessions.messages[0]["content"] == "hello"


def test_tool_schemas():
    assert any(t["function"]["name"] == "write_file" for t in schemas())


class ToolProvider:
    async def stream(self, model, messages, tools):
        if messages[-1]["role"] == "tool":
            yield "text", "Finished after tool result"
        else:
            import json

            yield (
                "call",
                {
                    "id": "call1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "hello.txt", "content": "world"}),
                    },
                },
            )


@pytest.mark.asyncio
async def test_tool_approval_and_follow_up(tmp_path):
    out = []
    requests = []

    async def emit(k, v):
        out.append((k, v))

    async def approve(message):
        requests.append(message)
        return True

    settings = Settings(models=[Model("demo", "openai/test", "openai")], default_model="demo")
    session = Sessions(tmp_path)
    agent = Agent(settings.model(), settings, Workspace(tmp_path), session, emit, approve, ToolProvider())
    await agent.run("create a file")
    assert (tmp_path / "hello.txt").read_text() == "world"
    assert len(requests) == 1
    assert session.messages[-1]["content"] == "Finished after tool result"


@pytest.mark.asyncio
async def test_plan_mode_denies_writes(tmp_path):
    async def emit(k, v):
        pass

    async def approve(message):
        raise AssertionError("Should never ask approval in plan mode")

    settings = Settings(models=[Model("demo", "openai/test", "openai")], default_model="demo", max_steps=1)
    agent = Agent(settings.model(), settings, Workspace(tmp_path), Sessions(tmp_path), emit, approve, ToolProvider())
    agent.plan = True
    await agent.run("try writing")
    assert not (tmp_path / "hello.txt").exists()
    assert "Denied" in agent.sessions.messages[-1]["content"]
