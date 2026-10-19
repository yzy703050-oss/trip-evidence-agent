"""CLI displays only the native application's authoritative final answer."""

import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.cli_session_resume import app_for


@pytest.mark.parametrize(
    "entrypoint",
    [
        "import cli",
        "from trip_assistant.presentation.facts import finalize",
        "from trip_assistant.bootstrap import create_application",
    ],
)
def test_entrypoints_import_in_a_fresh_python_process(entrypoint):
    result = subprocess.run(
        [sys.executable, "-c", entrypoint],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.asyncio
async def test_cli_only_displays_the_authoritative_final_answer(tmp_path):
    app = app_for(tmp_path)
    result = {
        "status": "completed",
        "final_answer": "由主Agent生成的完整答复。",
        "tool_results": [{"items": [{"price_cny": "999"}]}],
    }
    before = deepcopy(result)

    async def run(query):
        return result

    app.app = SimpleNamespace(run=run)
    assert await app.process_query("查询")
    assert app.console.file.getvalue().strip() == result["final_answer"]
    assert app.last_result is result and result == before
