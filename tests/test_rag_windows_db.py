"""A Windows Milvus Lite 3 index must not overwrite the legacy .db file."""

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentscope.message import Msg


class FakeEmbeddingModel:
    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, _text):
        class Vector:
            def tolist(self):
                return [0.1, 0.2]

        return Vector()


class FakeMilvusClient:
    def __init__(self, path, **_kwargs):
        self.path = path

    def has_collection(self, _name):
        return False

    def create_collection(self, **_kwargs):
        pass

    def close(self):
        pass


class FakeReleasedMilvusClient(FakeMilvusClient):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)
        self.loaded = False

    def has_collection(self, _name):
        return True

    def load_collection(self, _name):
        self.loaded = True

    def search(self, **_kwargs):
        if not self.loaded:
            raise RuntimeError("collection is released")
        return [[{"entity": {"id": 1, "content": "policy", "metadata": "{}"}, "distance": 0.9}]]


class FakeFailingMilvusClient(FakeReleasedMilvusClient):
    def search(self, **_kwargs):
        raise RuntimeError("index path failure")


class RagWindowsDatabaseTest(unittest.TestCase):
    def test_retrieval_error_is_not_reported_as_no_knowledge(self):
        agent_path = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ask-question"
            / "script"
            / "agent.py"
        )
        spec = importlib.util.spec_from_file_location("rag_agent_failure_test", agent_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "SentenceTransformer", return_value=FakeEmbeddingModel()),
            patch.object(module, "MilvusClient", FakeFailingMilvusClient),
        ):
            agent = module.RAGKnowledgeAgent(knowledge_base_path=directory, model=None)
            try:
                response = asyncio.run(
                    agent.reply(Msg(name="user", role="user", content="北京住宿标准？"))
                )
                self.assertEqual(json.loads(response.content)["status"], "error")
                self.assertIn("error", json.loads(response.content))
            finally:
                agent.close()

    def test_retrieved_document_keeps_full_evidence_for_evaluation(self):
        agent_path = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ask-question"
            / "script"
            / "agent.py"
        )
        spec = importlib.util.spec_from_file_location("rag_agent_evidence_test", agent_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "SentenceTransformer", return_value=FakeEmbeddingModel()),
            patch.object(module, "MilvusClient", FakeMilvusClient),
        ):
            agent = module.RAGKnowledgeAgent(knowledge_base_path=directory, model=None)
            try:
                agent.search_knowledge = lambda _query: [
                    {
                        "content": "前文" * 110 + "北京住宿上限500元",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    }
                ]
                response = asyncio.run(
                    agent.reply(Msg(name="user", role="user", content="北京住宿标准？"))
                )
                docs = json.loads(response.content)["retrieved_documents"]
                self.assertIn("500元", docs[0]["content"])
            finally:
                agent.close()

    def test_database_directory_override_avoids_unicode_index_path(self):
        agent_path = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ask-question"
            / "script"
            / "agent.py"
        )
        spec = importlib.util.spec_from_file_location("rag_agent_path_override_test", agent_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            safe_dir = Path(directory) / "ascii_rag"
            unicode_dir = Path(directory) / "中文目录"
            with (
                patch.dict(os.environ, {"V0_RAG_DB_DIR": str(safe_dir)}),
                patch.object(module, "SentenceTransformer", return_value=FakeEmbeddingModel()),
                patch.object(module, "MilvusClient", FakeMilvusClient),
            ):
                agent = module.RAGKnowledgeAgent(knowledge_base_path=str(unicode_dir), model=None)
            try:
                self.assertEqual(Path(agent._milvus_db_path).parent, safe_dir)
            finally:
                agent.close()

    def test_search_loads_existing_released_collection(self):
        agent_path = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ask-question"
            / "script"
            / "agent.py"
        )
        spec = importlib.util.spec_from_file_location("rag_agent_released_test_module", agent_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(module, "SentenceTransformer", return_value=FakeEmbeddingModel()),
                patch.object(module, "MilvusClient", FakeReleasedMilvusClient),
            ):
                agent = module.RAGKnowledgeAgent(knowledge_base_path=directory, model=None)
            try:
                results = agent.search_knowledge("住宿标准")
                self.assertEqual(results[0]["content"], "policy")
            finally:
                agent.close()

    def test_legacy_database_file_is_preserved(self):
        if __import__("os").name != "nt":
            self.skipTest("Windows-specific Milvus Lite storage format")

        agent_path = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ask-question"
            / "script"
            / "agent.py"
        )
        spec = importlib.util.spec_from_file_location("rag_agent_test_module", agent_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            legacy_db = Path(directory) / "milvus_lite.db"
            legacy_db.write_bytes(b"legacy database sentinel")
            with (
                patch.object(module, "SentenceTransformer", return_value=FakeEmbeddingModel()),
                patch.object(module, "MilvusClient", FakeMilvusClient),
            ):
                agent = module.RAGKnowledgeAgent(
                    knowledge_base_path=directory,
                    model=None,
                )
            try:
                self.assertTrue(agent.initialized)
                self.assertEqual(legacy_db.read_bytes(), b"legacy database sentinel")
                self.assertEqual(Path(agent._milvus_db_path).name, "milvus_lite_v3.db")
            finally:
                agent.close()


if __name__ == "__main__":
    unittest.main()
