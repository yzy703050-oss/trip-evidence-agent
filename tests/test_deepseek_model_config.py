"""The CLI must forward generation options through AgentScope's API."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-key")

from cli import TripEvidenceCLI  # noqa: E402
from config import LLM_CONFIG  # noqa: E402


class DeepSeekModelConfigTest(unittest.TestCase):
    def test_cli_applies_generation_settings(self):
        app = TripEvidenceCLI()
        with patch("cli.Prompt.ask", return_value="deepseek_config_test"):
            asyncio.run(app.initialize_system())
        self.assertEqual(app.model.model_name, "deepseek-flash")
        self.assertEqual(
            app.model.generate_kwargs,
            {
                "temperature": LLM_CONFIG["temperature"],
                "max_tokens": LLM_CONFIG["max_tokens"],
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )


def test_model_options_disable_deepseek_by_default_and_do_not_leak_mutations():
    from config import get_model_generate_kwargs
    first=get_model_generate_kwargs({'model_name':'deepseek-flash','temperature':0.7,'max_tokens':8192})
    assert first['extra_body']=={'thinking':{'type':'disabled'}}
    assert 'reasoning_effort' not in first
    first['extra_body']['thinking']['type']='enabled'
    assert get_model_generate_kwargs({'model_name':'deepseek-flash'})['extra_body']['thinking']['type']=='disabled'


def test_other_provider_does_not_receive_deepseek_specific_options():
    from config import get_model_generate_kwargs
    assert get_model_generate_kwargs({'model_name':'other-model','temperature':0,'max_tokens':5})=={'temperature':0,'max_tokens':5}


def test_eval_override_can_enable_thinking_without_conflicting_disabled_flag():
    from config import get_model_generate_kwargs
    value=get_model_generate_kwargs({'model_name':'deepseek-flash'},thinking='low')
    assert value['extra_body']=={'thinking':{'type':'enabled'}} and value['reasoning_effort']=='low'


if __name__ == "__main__":
    unittest.main()
