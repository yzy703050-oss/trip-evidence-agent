"""Consume complete SDK snapshots before allowing any tool execution."""
from dataclasses import dataclass, field
import json


@dataclass
class ModelTurn:
    text: str = ''
    tool_calls: list[dict] = field(default_factory=list)


async def collect_model_turn(response) -> ModelTurn:
    if hasattr(response, '__aiter__'):
        final = None
        async for snapshot in response:
            final = snapshot
        response = final
    if response is None:
        raise ValueError('empty model response')
    if isinstance(response, str):
        return ModelTurn(response)
    content = response.get('content') if isinstance(response, dict) else getattr(response, 'content', None)
    if content is None:
        text = response.get('text', '') if isinstance(response, dict) else getattr(response, 'text', '')
        return ModelTurn(text)
    if isinstance(content, str):
        return ModelTurn(content)
    turn, ids = ModelTurn(), set()
    for block in content:
        if block.get('type') == 'text':
            turn.text += block.get('text', '')
        elif block.get('type') == 'tool_use':
            call_id, name = block.get('id'), block.get('name')
            if not call_id or not name or call_id in ids:
                raise ValueError('invalid or duplicate tool call id')
            ids.add(call_id)
            arguments = json.loads(block['raw_input']) if 'raw_input' in block else block.get('input', {})
            if not isinstance(arguments, dict):
                raise ValueError('tool arguments must be an object')
            turn.tool_calls.append({'id': call_id, 'name': name, 'arguments': arguments})
    return turn


def to_assistant_tool_message(turn: ModelTurn) -> dict:
    return {'role': 'assistant', 'content': turn.text or None, 'tool_calls': [
        {'id': call['id'], 'type': 'function', 'function': {
            'name': call['name'], 'arguments': json.dumps(call['arguments'], ensure_ascii=False)}}
        for call in turn.tool_calls]}


def to_tool_message(call_id: str, result: dict) -> dict:
    return {'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result, ensure_ascii=False)}
