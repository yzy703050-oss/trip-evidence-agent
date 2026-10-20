"""Latency metrics and offline PowerShell reporting contracts."""

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.latency import summarize_latency

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_latency.ps1"
SHELL = shutil.which("powershell.exe")
requires_powershell = pytest.mark.skipif(SHELL is None, reason="Windows PowerShell required")


def test_latency_distinguishes_parallel_service_time_from_wall_time():
    calls = [
        dict(stage="main:plan", latency_ms=1000, input_tokens=100, output_tokens=50, ttft_ms=200),
        dict(stage="main:step", latency_ms=2000, input_tokens=200, output_tokens=100),
        dict(stage="agent:information_query", latency_ms=3000, input_tokens=300, output_tokens=150),
    ]
    tools = [
        dict(name="train_search", elapsed_ms=100, cache_hit=False),
        dict(name="hotel_search", elapsed_ms=100, cache_hit=False),
    ]
    report = summarize_latency(calls, tools, total_ms=6250, external_requests=2)
    assert report["model_total_ms"] == 6000 and report["end_to_end_ms"] == 6250
    assert report["slowest_stage"] == "agent:information_query"
    assert report["stages"]["main:plan"]["average_ms"] == 1000
    assert report["tool_service_sum_ms"] == 200
    assert report["non_model_wall_ms"] == 250
    assert report["output_tokens_per_second"] == 50


async def test_stream_latency_records_first_chunk_and_total_response_time():
    from types import SimpleNamespace

    from trip_assistant.runtime.telemetry import MeteredModel

    rows = []

    async def model(messages):
        async def chunks():
            yield SimpleNamespace(content="a", usage=None)
            yield SimpleNamespace(content="ab", usage={"input_tokens": 10, "output_tokens": 2})

        return chunks()

    response = await MeteredModel(model, rows.append)([])
    async for _chunk in response:
        pass
    assert 0 <= rows[0]["ttft_ms"] <= rows[0]["latency_ms"]
    assert rows[0]["output_tokens"] == 2


def invoke(tmp_path, *, failure=False):
    records = []
    for case in ("query_price", "default_date", "multi_route_unspecified_stay"):
        records.append(
            dict(
                case=case,
                status="error" if failure else "completed",
                assessment={"passed": not failure},
                latency=dict(
                    end_to_end_ms=1200,
                    model_calls=3,
                    model_total_ms=1000,
                    tool_service_sum_ms=2,
                    stages={
                        "main:plan": {"total_ms": 200},
                        "main:step": {"total_ms": 300},
                        "agent:information_query": {"total_ms": 500},
                    },
                ),
            )
        )
    del records[0]["latency"]["stages"]["main:step"]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(records), encoding="utf-8")
    fake = tmp_path / "fake-python.ps1"
    fake.write_text(
        """$argumentPath = Join-Path $PSScriptRoot 'args.json'
$args | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $argumentPath
$index = [Array]::IndexOf($args, '--output')
$out = $args[$index + 1]
New-Item -ItemType Directory -Force -Path $out | Out-Null
$fixturePath = Join-Path $PSScriptRoot 'fixture.json'
$reportPath = Join-Path $out 'report.json'
Copy-Item -LiteralPath $fixturePath -Destination $reportPath
Write-Output 'START query_price'
$global:LASTEXITCODE = """
        + ("1" if failure else "0")
        + "\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            SHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-PythonPath",
            str(fake),
            "-OutputRoot",
            str(tmp_path / "outputs"),
            "-Label",
            "no-vpn",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
    )
    return result


@requires_powershell
def test_three_cases_and_summary_from_any_working_directory(tmp_path):
    result = invoke(tmp_path)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    args = json.loads((tmp_path / "args.json").read_text(encoding="utf-8-sig"))
    start, end = args.index("--cases"), args.index("--thinking")
    assert set(args[start + 1 : end]) == {
        "query_price",
        "default_date",
        "multi_route_unspecified_stay",
    }
    assert args[end + 1] == "disabled"
    output = next((tmp_path / "outputs").iterdir())
    summary = (output / "summary.md").read_text(encoding="utf-8-sig")
    assert "no-vpn" in summary and "1.200" in summary and "simulation" in summary
    with (output / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["MainLoop_s"] == "0.000"
    assert rows[1]["MainLoop_s"] == "0.300"
    assert all(
        row["Total_s"] == "1.200"
        and row["Initial_s"] == "0.200"
        and row["Information_s"] == "0.500"
        for row in rows
    )
    assert (output / "run.log").exists()
    result = invoke(tmp_path)
    assert result.returncode == 0 and len(list((tmp_path / "outputs").iterdir())) == 2


@requires_powershell
def test_failed_evaluation_keeps_report_and_returns_nonzero(tmp_path):
    result = invoke(tmp_path, failure=True)
    assert result.returncode != 0
    output = next((tmp_path / "outputs").iterdir())
    assert (output / "summary.md").exists() and (output / "report.json").exists()
