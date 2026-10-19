"""OpenAI-compatible SDK adapter: expose only fully collected, validated turns."""

import inspect
import json
from dataclasses import dataclass
from time import perf_counter

from openai import AsyncOpenAI


@dataclass
class ModelResponse:
    content: list
    usage: dict | None
    ttft_ms: float | None = None
    diagnostics: dict | None = None


def _usage(value):
    if value is None:
        return None

    def field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    total = field(value, "prompt_tokens")
    hit = field(value, "prompt_cache_hit_tokens")
    if hit is None:
        hit = field(field(value, "prompt_tokens_details"), "cached_tokens")
    miss = field(value, "prompt_cache_miss_tokens")
    if miss is None and hit is not None and total is not None:
        miss = total - hit
    result = {
        "input_tokens": total,
        "output_tokens": field(value, "completion_tokens"),
        "cached_input_tokens": hit,
        "uncached_input_tokens": miss,
    }
    reasoning = field(field(value, "completion_tokens_details"), "reasoning_tokens")
    if reasoning is not None:
        result["reasoning_tokens"] = reasoning
    return result


class ModelProtocolError(ValueError):
    def __init__(self, message, usage, *, finish_reason):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.usage = _usage(usage)


class IncompleteModelResponse(ModelProtocolError):
    def __init__(self, finish_reason, usage):
        super().__init__(
            f"model response did not finish completely: {finish_reason}",
            usage,
            finish_reason=finish_reason,
        )


def _snapshot(text, calls, finish, usage, ttft, *, reasoning_chars=None):
    diagnostics = {
        "finish_reason": finish,
        "text_chars": len(text),
        "tool_arguments_chars": sum(len(call.get("arguments") or "") for call in calls),
        "reasoning_chars": reasoning_chars,
    }
    if finish not in ("stop", "tool_calls"):
        error = IncompleteModelResponse(finish, usage)
        error.diagnostics = diagnostics
        raise error
    blocks = [{"type": "text", "text": text}] if text else []
    ids = set()
    for call in calls:
        if not call["id"] or not call["name"] or call["id"] in ids:
            raise ModelProtocolError("invalid or duplicate tool call", usage, finish_reason=finish)
        ids.add(call["id"])
        try:
            arguments = json.loads(call["arguments"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ModelProtocolError(
                "native tool arguments are not valid JSON", usage, finish_reason=finish
            ) from error
        if not isinstance(arguments, dict):
            raise ModelProtocolError(
                "tool arguments must be an object", usage, finish_reason=finish
            )
        blocks.append(
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments}
        )
    if calls and finish != "tool_calls":
        raise ModelProtocolError(
            "tool response missing completion marker", usage, finish_reason=finish
        )
    return ModelResponse(blocks, _usage(usage), ttft, diagnostics)


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
                choice.message.content or "",
                calls,
                choice.finish_reason,
                response.usage,
                None,
                reasoning_chars=len(getattr(choice.message, "reasoning_content", None) or ""),
            )
        text, calls, finish, usage, ttft = "", {}, None, None, None
        reasoning_chars = 0
        try:
            async for chunk in response:
                if chunk.usage is not None:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
                    delta = choice.delta
                    reasoning_chars += len(getattr(delta, "reasoning_content", None) or "")
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
        return _snapshot(
            text,
            [calls[i] for i in sorted(calls)],
            finish,
            usage,
            ttft,
            reasoning_chars=reasoning_chars,
        )
