"""Load the existing policy retriever only when policy is requested."""

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path


class LazyPolicyRetriever:
    def __init__(self, model, request=None):
        self.model = model
        self.backend = None
        self.request = request
        self.lock = asyncio.Lock()

    @property
    def loaded(self):
        return self.backend is not None

    def _load(self):
        path = Path(__file__).resolve().parents[2] / ".claude/skills/ask-question/script/agent.py"
        spec = importlib.util.spec_from_file_location("skills.ask_question.agent", path)
        if spec is None or spec.loader is None:
            raise ImportError("policy retrieval unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.RAGKnowledgeAgent(name="travel_policy", model=self.model)

    async def search(self, query, limit=5):
        async with self.lock:
            if self.backend is None:
                self.backend = await asyncio.to_thread(self._load)

        async def fetch():
            service = getattr(self.backend, "rag_service", None)
            if service is not None:
                value = service.search(query, limit=limit)
                return await value if inspect.isawaitable(value) else value
            result = await asyncio.to_thread(self.backend.search_knowledge, query, top_k=limit)
            if getattr(self.backend, "_last_search_error", None):
                raise ConnectionError("policy retrieval failed")
            return result

        return await self.request(fetch) if self.request else await fetch()
