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
    class Info:
        async def run(self, context, run):
            domains = {"train": domain([train()])}
            run.domain_results.update(domains)
            return {
                "status": "ok",
                "summary": "已查到车次",
                "missing_fields": [],
                "domain_results": domains,
                "travel_conditions": {},
            }

    main = Main([{"agent_name": "information_query", "requested_domains": ["train"]}])
    result = await ExecutionHarness(main, {"information_query": Info()}).run_turn(
        {"original_query": "查询火车"}, RunState("t1")
    )
    final = json.loads(json.dumps(result, ensure_ascii=False))
    assert final["finalization_method"] == "forward"
    assert main.finalize_calls == 0
    for expected in (
        "G25",
        "北京南",
        "上海虹桥",
        "627.50",
        "单人票价",
        "查询时有票",
        SOURCE["url"],
        SOURCE["fetched_at"],
    ):
        assert expected in final["final_answer"]
    assert final["domain_results"]["train"]["items"][0]["price_cny"] == "627.50"


@pytest.mark.asyncio
async def test_workflow_final_answer_contains_dates_and_unknown_costs(tmp_path):
    result = await Runtime(tmp_path).turn()
    assert result["status"] == "completed"
    for expected in (
        "北京",
        "杭州",
        "2026-10-15",
        "入住日期",
        "离店日期",
        "房价、空房和入住规则尚未核实",
        "全程费用尚未完整核实",
        "参考链接",
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
