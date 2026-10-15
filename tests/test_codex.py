from types import SimpleNamespace

import pytest

from fluxen.codex import CodexAgent
from fluxen.config import Settings


def make_agent(tmp_path, permission="ask"):
    events = []
    approvals = []

    async def emit(kind, value):
        events.append((kind, value))

    async def approve(value):
        approvals.append(value)
        return True

    agent = CodexAgent(
        None,
        Settings(permission=permission),
        SimpleNamespace(root=tmp_path, last_diff=""),
        SimpleNamespace(dir=tmp_path, id="test"),
        emit,
        approve,
    )
    agent.reply = ""
    return agent, events, approvals


@pytest.mark.asyncio
async def test_stream_and_diff(tmp_path):
    agent, events, _ = make_agent(tmp_path)
    await agent.event({"method": "item/agentMessage/delta", "params": {"delta": "Hello"}})
    await agent.event({"method": "turn/diff/updated", "params": {"diff": "a diff"}})
    assert agent.reply == "Hello"
    assert events == [("token", "Hello")]
    assert agent.workspace.last_diff == "a diff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission,plan,decision", [("ask", False, "accept"), ("read-only", False, "decline"), ("ask", True, "decline")]
)
async def test_approval_policy(tmp_path, permission, plan, decision):
    agent, _, approvals = make_agent(tmp_path, permission)
    agent.plan = plan
    sent = []

    async def send(message):
        sent.append(message)

    agent.send = send
    await agent.event({"id": 20, "method": "item/commandExecution/requestApproval", "params": {"command": "echo hi"}})
    assert sent[0]["result"]["decision"] == decision
    assert bool(approvals) == (decision == "accept")


@pytest.mark.asyncio
async def test_missing_codex_is_actionable(tmp_path, monkeypatch):
    agent, _, _ = make_agent(tmp_path)
    monkeypatch.setattr("fluxen.codex.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="codex login"):
        await agent.run("hello")
