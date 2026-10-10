[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')]
    [string]$Label = 'no-vpn',
    [string]$PythonPath = '',
    [string]$OutputRoot = ''
)

# User controls VPN/proxy settings. Label is descriptive, never auto-detected.
# Real configured LLM, explicitly simulated train/hotel providers, three cases.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $repoRoot 'data/evals' }
if (-not $PythonPath) {
    $sharedRoot = Split-Path -Parent (Split-Path -Parent $repoRoot)
    foreach ($candidate in @((Join-Path $repoRoot '.venv/Scripts/python.exe'),
                             (Join-Path $sharedRoot '.venv/Scripts/python.exe'))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $PythonPath = $candidate; break }
    }
    if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
}

$suffix = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$output = Join-Path $OutputRoot ('latency-' + $Label + '-' + $suffix)
New-Item -ItemType Directory -Path $output -Force | Out-Null
$output = (Resolve-Path -LiteralPath $output).Path
$log = Join-Path $output 'run.log'
$cases = @('query_price', 'default_date', 'multi_route_unspecified_stay')
$evalArgs = @('-m', 'evals.run_preflight_simulated', '--cases') + $cases +
    @('--thinking', 'disabled', '--output', $output)
Write-Host ('Network label (manual): ' + $Label)
Write-Host 'Three cases. Thinking: disabled. LLM: real. Train/hotel: simulation.'
Write-Host ('Output: ' + $output)

$oldPythonEncoding = $env:PYTHONIOENCODING
$oldConsoleEncoding = [Console]::OutputEncoding
$evalExit = 1
Push-Location -LiteralPath $repoRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
    # Native stderr warnings must be logged, not treated as terminating errors.
    $ErrorActionPreference = 'Continue'
    & $PythonPath @evalArgs 2>&1 |
        ForEach-Object {
            $line = $_.ToString()
            Add-Content -LiteralPath $log -Value $line -Encoding UTF8
            if ($line.StartsWith('START ')) { Write-Host $line }
        }
    $evalExit = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = 'Stop'
    $env:PYTHONIOENCODING = $oldPythonEncoding
    [Console]::OutputEncoding = $oldConsoleEncoding
    Pop-Location
}

$reportPath = Join-Path $output 'report.json'
if (-not (Test-Path -LiteralPath $reportPath)) {
    Write-Host ('No report was produced. Check: ' + $log)
    exit 1
}
# PS 5.1 emits a JSON array as one pipeline object; assignment unwraps it.
$records = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
function Seconds($milliseconds) {
    return ([double]$milliseconds / 1000).ToString('F3', [Globalization.CultureInfo]::InvariantCulture)
}
function StageSeconds($stages, $name) {
    $entry = $stages.PSObject.Properties[$name]
    if ($null -eq $entry) { return '0.000' }
    return Seconds $entry.Value.total_ms
}
$rows = @($records | ForEach-Object {
    $latency = $_.latency
    [PSCustomObject]@{
        Case = $_.case; Status = $_.status; Passed = [bool]$_.assessment.passed
        Total_s = Seconds $latency.end_to_end_ms; ModelCalls = $latency.model_calls
        Initial_s = StageSeconds $latency.stages 'main:plan'
        MainLoop_s = StageSeconds $latency.stages 'main:step'
        Information_s = StageSeconds $latency.stages 'agent:information_query'
        Model_s = Seconds $latency.model_total_ms
        ToolService_s = Seconds $latency.tool_service_sum_ms
    }
})
$rows | Format-Table Case, Status, Passed, Total_s, ModelCalls, Initial_s, MainLoop_s, Information_s -AutoSize | Out-Host
$rows | Export-Csv -LiteralPath (Join-Path $output 'summary.csv') -NoTypeInformation -Encoding UTF8
$markdown = @(
    '# Three-case latency measurement', '',
    ('Network label (manual): ' + $Label),
    'LLM: real configured model; train/hotel: simulation; thinking: disabled.',
    'Times in seconds. VPN and proxy configuration are not changed or verified.',
    'A single sample is not a percentile benchmark. Tool service times may overlap.', '',
    '| Case | Status | Passed | Total | Model calls | Initial | Main loop | Information | Model total | Tool service |',
    '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'
)
foreach ($row in $rows) {
    $markdown += '| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} |' -f
        $row.Case, $row.Status, $row.Passed, $row.Total_s, $row.ModelCalls,
        $row.Initial_s, $row.MainLoop_s, $row.Information_s, $row.Model_s, $row.ToolService_s
}
$markdown += @('', 'Detailed records: report.json, latency.csv, per-case JSON, memory/, run.log.')
$markdown | Set-Content -LiteralPath (Join-Path $output 'summary.md') -Encoding UTF8
Write-Host ('Summary: ' + (Join-Path $output 'summary.md'))
if ($evalExit -ne 0 -or $records.Count -ne 3 -or @($rows | Where-Object { -not $_.Passed }).Count -gt 0) {
    Write-Host 'Some cases failed or are missing. Timing records are preserved; inspect run.log and case JSON.'
    exit 1
}
exit 0
