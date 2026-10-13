"""The final JSON owns the full reply; CLI does not add facts from other fields."""

import io
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from rich.console import Console
from test_cli_prose import SOURCE, domain, train
from test_main_harness import Main
from workflow_runtime import Runtime

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from cli import TripEvidenceCLI


@pytest.mark.parametrize(
    "entrypoint",
    [
        "from cli import TripEvidenceCLI",
        "from utils.response_renderer import finalize_business_result",
    ],
)
def test_entrypoints_import_in_a_fresh_python_process(entrypoint):
    result = subprocess.run(
        [sys.executable, "-c", entrypoint],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_harness_final_answer_contains_sourced_quote_without_cli():
    from agents.workflow_queries import as_result
    class Provider:
        async def search(self, query):
            return as_result({**domain([train()]), 'parameters': query.to_dict()})
    class DirectMain:
        finalize_calls = 0
        async def initialize(self, context):
            return {'response_mode': 'answer', 'agent_schedule': [], 'query_plan': [{'domain': 'train',
                'parameters': {'origin': '北京南', 'destination': '上海虹桥', 'departure_date': '2026-10-11'}}]}
    class Registry(dict):
        providers = {'train_search': Provider()}
    main = DirectMain()
    result = await ExecutionHarness(main, Registry()).run_turn({'original_query': '查询火车'}, RunState('t1'))
    final = json.loads(json.dumps(result, ensure_ascii=False))
    assert final["finalization_method"] == "tools"
    assert main.finalize_calls == 0
    for expected in (
        "G25",
        "北京南",
        "上海虹桥",
        "627.50",
        "单人票价",
        "查询时有票",
    ):
        assert expected in final["final_answer"]
    assert SOURCE["url"] not in final["final_answer"]
    assert final["domain_results"]["train"]["items"][0]["source"] == SOURCE
    assert final["domain_results"]["train"]["items"][0]["price_cny"] == "627.50"


@pytest.mark.asyncio
async def test_workflow_final_answer_contains_dates_and_unknown_costs(tmp_path):
    result = await Runtime(tmp_path).turn()
    assert result["status"] == "completed"
    for expected in (
        "北京",
        "杭州",
        "2026年10月15日",
        "入住",
        "离店",
        "酒店房价、空房和入住及退改规则需在订房时确认",
        "其余费用还需确认",
    ):
        assert expected in result["final_answer"]
    assert isinstance(result["workflow"], dict) and isinstance(result["validated_plan"], dict)


def test_cli_only_displays_the_authoritative_final_answer():
    app = TripEvidenceCLI()
    output = io.StringIO()
    app.console = Console(file=output, width=1000, color_system=None)
    result = {
        "status": "ok",
        "final_answer": "完整回复由最终结果生成阶段负责。",
        "domain_results": {"train": domain([train(price_cny="999")])},
    }
    before = deepcopy(result)
    app._display_results(result)
    assert output.getvalue().strip() == result["final_answer"]
    assert result == before
