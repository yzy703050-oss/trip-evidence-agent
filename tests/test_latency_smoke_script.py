import csv
import json
from pathlib import Path
import shutil
import subprocess
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'test_latency.ps1'
SHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(SHELL is None, reason='Windows PowerShell required')


def invoke(tmp_path, *, failure=False):
    records = []
    for case in ('query_price', 'default_date', 'multi_route_unspecified_stay'):
        records.append(dict(case=case, status='error' if failure else 'completed',
            assessment={'passed': not failure}, latency=dict(end_to_end_ms=1200,
                model_calls=3, model_total_ms=1000, tool_service_sum_ms=2,
                stages={'main:plan': {'total_ms': 200}, 'main:step': {'total_ms': 300},
                        'agent:information_query': {'total_ms': 500}})))
    del records[0]['latency']['stages']['main:step']
    fixture = tmp_path / 'fixture.json'
    fixture.write_text(json.dumps(records), encoding='utf-8')
    fake = tmp_path / 'fake-python.ps1'
    fake.write_text("""$args | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'args.json')
$index = [Array]::IndexOf($args, '--output')
$out = $args[$index + 1]
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'fixture.json') -Destination (Join-Path $out 'report.json')
Write-Output 'START query_price'
$global:LASTEXITCODE = """ + ('1' if failure else '0') + '\n', encoding='utf-8-sig')
    result = subprocess.run([SHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
                             '-PythonPath', str(fake), '-OutputRoot', str(tmp_path/'outputs'), '-Label', 'no-vpn'],
                            cwd=tmp_path, capture_output=True, timeout=30)
    return result


def test_three_cases_and_summary_from_any_working_directory(tmp_path):
    result = invoke(tmp_path)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    args = json.loads((tmp_path/'args.json').read_text(encoding='utf-8-sig'))
    start, end = args.index('--cases'), args.index('--thinking')
    assert set(args[start+1:end]) == {'query_price','default_date','multi_route_unspecified_stay'}
    assert args[end+1] == 'disabled'
    output = next((tmp_path/'outputs').iterdir())
    summary = (output/'summary.md').read_text(encoding='utf-8-sig')
    assert 'no-vpn' in summary and '1.200' in summary and 'simulation' in summary
    with (output/'summary.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]['MainLoop_s'] == '0.000'
    assert rows[1]['MainLoop_s'] == '0.300'
    assert all(row['Total_s'] == '1.200' and row['Initial_s'] == '0.200'
               and row['Information_s'] == '0.500' for row in rows)
    assert (output/'run.log').exists()
    result = invoke(tmp_path)
    assert result.returncode == 0 and len(list((tmp_path/'outputs').iterdir())) == 2


def test_failed_evaluation_keeps_report_and_returns_nonzero(tmp_path):
    result = invoke(tmp_path, failure=True)
    assert result.returncode != 0
    output = next((tmp_path/'outputs').iterdir())
    assert (output/'summary.md').exists() and (output/'report.json').exists()
