# Starts the local TheoremExplainAgent web app.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$port = if ($env:TEA_WEB_PORT) { $env:TEA_WEB_PORT } else { "8765" }

if (-not (Test-Path $python)) {
    Write-Error "找不到虚拟环境：$python`n请先创建 .venv 并安装依赖。"
}

$env:PYTHONPATH = $root
$env:PYTHONUTF8 = "1"
if (-not $env:TEA_MANIM_QUALITY) { $env:TEA_MANIM_QUALITY = "-qm" }

Write-Host "TheoremExplainAgent Web  ->  http://127.0.0.1:$port" -ForegroundColor Cyan
& $python -m uvicorn webapp.app:app --host 127.0.0.1 --port $port
