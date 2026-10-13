"""Small project-owned message protocol; no orchestration framework dependency."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Msg:
    name: str
    content: Any
    role: str = "assistant"


class AgentBase:
    def __init__(self, name=None, **kwargs):
        self.name = name or type(self).__name__
