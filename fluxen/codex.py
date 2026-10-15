"""Codex app-server backend using its local stdio protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from types import SimpleNamespace

from . import __version__


class CodexAgent:
    def __init__(self, model, settings, workspace, sessions, emit, approve):
        self.model = SimpleNamespace(name=model or "codex", model=model)
        self.settings = settings
        self.workspace = workspace
        self.sessions = sessions
        self.emit = emit
        self.approve = approve
        self.plan = False
        self.usage = [0, 0]
        self.proc = None
        self.loaded_thread = None  # thread already started/resumed in the running app-server
        self.turn_id = None
        self.sequence = 0
        self.items = {}
        self.reply = ""
        self.finished = None
        self.load_thread()

    @property
    def state_path(self):
        return self.sessions.dir / (self.sessions.id + ".codex")

    def load_thread(self):
        """Pick up the Codex thread saved for the current Fluxen session, if any."""
        self.thread_id = None
        if self.state_path.exists():
            self.thread_id = self.state_path.read_text(encoding="utf-8").strip() or None

    async def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def receive(self):
        line = await self.proc.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server exited. Check codex login and your Codex installation.")
        return json.loads(line)

    async def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        await self.send(dict(id=identifier, method=method, params=params))
        while True:
            message = await self.receive()
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                return message.get("result", {})
            await self.event(message)

    async def event(self, message):
        method = message.get("method", "")
        params = message.get("params", {})
        if "id" in message and method:
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                allowed = False
                if not self.plan and self.settings.permission != "read-only":
                    preview = dict(params)
                    if params.get("itemId") in self.items:
                        preview["item"] = self.items[params["itemId"]]
                    allowed = await self.approve(method + "\n" + json.dumps(preview, ensure_ascii=False, indent=2))
                await self.send({"id": message["id"], "result": {"decision": "accept" if allowed else "decline"}})
            elif method == "item/permissions/requestApproval":
                await self.send({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
            else:
                await self.send(
                    {"id": message["id"], "error": {"code": -32601, "message": "Unsupported Fluxen client request"}}
                )
            return
        if method == "item/agentMessage/delta":
            text = params.get("delta", "")
            self.reply += text
            await self.emit("token", text)
        elif method in ("item/started", "item/completed"):
            item = params.get("item", {})
            if item.get("id"):
                self.items[item["id"]] = item
            if item.get("type") not in ("agentMessage", "userMessage"):
                await self.emit("tool", item.get("type", "tool") + ": " + item.get("status", "started"))
        elif method == "turn/diff/updated":
            self.workspace.last_diff = params.get("diff", "")
        elif method == "error":
            await self.emit("error", params.get("error", {}).get("message", "Codex error"))
        elif method == "turn/started":
            self.turn_id = params.get("turn", {}).get("id")
        elif method == "turn/completed":
            self.finished = params.get("turn", {})

    async def start(self):
        """Start the app-server once and reuse it for later turns while it stays healthy."""
        if self.proc is not None and self.proc.returncode is None:
            return
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("Codex CLI is not installed or on PATH. Install Codex and run codex login first.")
        await self.emit("status", "Starting Codex")
        self.loaded_thread = None
        self.proc = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            cwd=self.workspace.root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        try:
            info = {"name": "fluxen", "title": "Fluxen", "version": __version__}
            await asyncio.wait_for(self.request("initialize", {"clientInfo": info}), 30)
            await self.send({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise

    async def close(self):
        proc, self.proc = self.proc, None
        self.loaded_thread = None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            proc.stdin.close()
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def run(self, prompt):
        self.reply = ""
        self.finished = None
        self.turn_id = None
        await self.start()
        self.sessions.messages.append({"role": "user", "content": prompt})
        self.sessions.save()
        healthy = False
        try:
            readonly = self.plan or self.settings.permission == "read-only"
            approval = "never" if readonly else "untrusted"
            if not self.thread_id or self.loaded_thread != self.thread_id:
                options = {
                    "cwd": str(self.workspace.root),
                    "approvalPolicy": approval,
                    "sandbox": "read-only" if readonly else "workspace-write",
                }
                if self.model.model:
                    options["model"] = self.model.model
                if self.thread_id:
                    options["threadId"] = self.thread_id
                    result = await self.request("thread/resume", options)
                else:
                    result = await self.request("thread/start", options)
                self.thread_id = result["thread"]["id"]
                self.loaded_thread = self.thread_id
                self.state_path.write_text(self.thread_id, encoding="utf-8")
            policy = (
                {"type": "readOnly"}
                if readonly
                else {"type": "workspaceWrite", "writableRoots": [str(self.workspace.root)], "networkAccess": False}
            )
            text = prompt
            if self.plan:
                text = "Planning only: inspect and explain a plan without changing files. " + prompt
            turn = {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": approval,
                "sandboxPolicy": policy,
            }
            if self.model.model:
                turn["model"] = self.model.model
            result = await self.request("turn/start", turn)
            self.turn_id = result["turn"]["id"]
            while self.finished is None:
                await self.event(await self.receive())
            healthy = True
            status = self.finished.get("status")
            if status != "completed":
                raise RuntimeError(
                    "Codex turn " + str(status) + ": " + str(self.finished.get("error") or "interrupted")
                )
        except asyncio.CancelledError:
            if self.thread_id and self.turn_id and self.proc is not None:
                with contextlib.suppress(Exception):
                    params = {"threadId": self.thread_id, "turnId": self.turn_id}
                    await asyncio.wait_for(self.request("turn/interrupt", params), 5)
            raise
        finally:
            if self.reply:
                self.sessions.messages.append({"role": "assistant", "content": self.reply})
            self.sessions.save()
            # After a cancel or protocol error, late events from the old turn could confuse the
            # next one, so start a fresh app-server next time. Completed turns keep it running.
            if not healthy:
                await self.close()
