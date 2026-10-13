"""OpenAI-compatible SDK adapter: expose only fully collected, validated turns."""

from dataclasses import dataclass
import inspect
import json
from time import perf_counter

from openai import AsyncOpenAI


@dataclass
class ModelResponse:
    content: list
    usage: dict | None
    ttft_ms: float | None = None


def _usage(value):
    if value is None:
        return None
    return {"input_tokens": value.prompt_tokens, "output_tokens": value.completion_tokens}


def _snapshot(text, calls, finish, usage, ttft):
    if finish not in ("stop", "tool_calls"):
        raise ValueError("model response did not finish completely")
    blocks = [{"type": "text", "text": text}] if text else []
    ids = set()
    for call in calls:
        if not call["id"] or not call["name"] or call["id"] in ids:
            raise ValueError("invalid or duplicate tool call")
        ids.add(call["id"])
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        blocks.append(
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments}
        )
    if calls and finish != "tool_calls":
        raise ValueError("tool response missing completion marker")
    return ModelResponse(blocks, _usage(usage), ttft)


class OpenAIChatModel:
    def __init__(
        self,
        model_name,
        api_key=None,
        *,
        client_kwargs=None,
        generate_kwargs=None,
        stream=True,
        client=None,
        **kwargs,
    ):
        self.model_name = model_name
        self.generate_kwargs = dict(generate_kwargs or {})
        self.stream = stream
        options = dict(client_kwargs or {})
        options["max_retries"] = 0  # Retry ownership stays with the project's outer wrapper.
        self.client = client or AsyncOpenAI(api_key=api_key, **options)

    async def __call__(self, messages, **kwargs):
        options = {**self.generate_kwargs, **kwargs}
        stream = options.pop("stream", self.stream)
        if stream:
            options["stream_options"] = {**options.get("stream_options", {}), "include_usage": True}
        started = perf_counter()
        response = await self.client.chat.completions.create(
            model=self.model_name, messages=messages, stream=stream, **options
        )
        if not stream:
            choice = response.choices[0]
            calls = [
                {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                for c in choice.message.tool_calls or []
            ]
            return _snapshot(
                choice.message.content or "", calls, choice.finish_reason, response.usage, None
            )
        text, calls, finish, usage, ttft = "", {}, None, None, None
        try:
            async for chunk in response:
                if chunk.usage is not None:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
                    delta = choice.delta
                    if delta.content or delta.tool_calls:
                        if ttft is None:
                            ttft = round((perf_counter() - started) * 1000, 3)
                    text += delta.content or ""
                    for fragment in delta.tool_calls or []:
                        call = calls.setdefault(
                            fragment.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if fragment.id:
                            if call["id"] and call["id"] != fragment.id:
                                raise ValueError("tool identity changed during stream")
                            call["id"] = fragment.id
                        if fragment.function:
                            call["name"] += fragment.function.name or ""
                            call["arguments"] += fragment.function.arguments or ""
        finally:
            close = getattr(response, "close", None) or getattr(response, "aclose", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result
        return _snapshot(text, [calls[i] for i in sorted(calls)], finish, usage, ttft)
