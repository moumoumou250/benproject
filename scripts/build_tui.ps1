# 构建 Go 版 Bubble Tea TUI（Windows），输出到 Ben\bin\ben-tui.exe。
#
# 运行时解析顺序（Ben/interface/tui/runtime.py::binary_command）：
#   1. 仓库内 go.mod 存在且 go 可用 → `go run ./cmd/ben-tui`（开发态，直接用源码）
#   2. 否则回落到 Ben\bin\ben-tui.exe（打包态）
# 因此本脚本用于「让打包态也带上最新二进制」，或给没有 Go 工具链的机器预编译。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$tuiDir = Join-Path $projectRoot 'Ben\interface\tui'
$outDir = Join-Path $projectRoot 'Ben\bin'
$outFile = Join-Path $outDir 'ben-tui.exe'

$go = Get-Command go -ErrorAction SilentlyContinue
if (-not $go) {
    Write-Error '未找到 go。构建 TUI 需要 Go 1.24 或更新版本。'
    exit 1
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Host "构建 ben-tui.exe ..."
Push-Location $tuiDir
try {
    $env:CGO_ENABLED = '0'
    & go build -trimpath -ldflags='-s -w' -o $outFile ./cmd/ben-tui
    if ($LASTEXITCODE -ne 0) { throw "go build 失败（exit=$LASTEXITCODE）" }
}
finally {
    Pop-Location
}

$size = [math]::Round((Get-Item $outFile).Length / 1MB, 1)
Write-Host "完成：$outFile（${size} MB）"
