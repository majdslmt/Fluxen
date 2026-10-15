"""One streaming implementation for all LiteLLM-supported providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .config import Model


@dataclass
class Completion:
    content: str = ""
    calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class Provider:
    async def stream(self, model: Model, messages: list[dict], tools: list[dict]) -> AsyncIterator[tuple[str, Any]]:
        from litellm import acompletion

        options = model.options()
        kwargs = dict(messages=messages, stream=True, stream_options={"include_usage": True}, **options)
        if tools:
            kwargs["tools"] = tools
        # Some providers do not support usage streaming; retry without that argument.
        try:
            stream = await acompletion(**kwargs)
        except Exception as e:
            if "stream_options" not in str(e).lower():
                raise
            kwargs.pop("stream_options", None)
            stream = await acompletion(**kwargs)
        pending: dict[int, dict] = {}
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                yield "usage", (getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield "text", content
            for call in getattr(delta, "tool_calls", None) or []:
                index = getattr(call, "index", 0) or 0
                p = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if getattr(call, "id", None):
                    p["id"] = call.id
                fn = getattr(call, "function", None)
                if fn:
                    p["name"] += getattr(fn, "name", None) or ""
                    p["arguments"] += getattr(fn, "arguments", None) or ""
        for idx in sorted(pending):
            p = pending[idx]
            yield (
                "call",
                {
                    "id": p["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": p["name"], "arguments": p["arguments"]},
                },
            )
