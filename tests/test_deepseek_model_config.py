"""The CLI must forward generation options through AgentScope's API."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-key")

from cli import AligoCLI  # noqa: E402
from config import LLM_CONFIG  # noqa: E402


class DeepSeekModelConfigTest(unittest.TestCase):
    def test_cli_applies_generation_settings(self):
        app = AligoCLI()
        with patch("cli.Prompt.ask", return_value="deepseek_config_test"):
            asyncio.run(app.initialize_system())
        self.assertEqual(app.model.model_name, "deepseek-flash")
        self.assertEqual(
            app.model.generate_kwargs,
            {
                "temperature": LLM_CONFIG["temperature"],
                "max_tokens": LLM_CONFIG["max_tokens"],
            },
        )


if __name__ == "__main__":
    unittest.main()
