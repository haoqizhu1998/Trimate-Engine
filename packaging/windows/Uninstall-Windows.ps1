$ErrorActionPreference = 'Stop'
$app = Join-Path $env:LOCALAPPDATA 'AthleteGrowth'
$answer = Read-Host '卸载Trimate程序？个人训练数据会保留。(y/N)'
if ($answer -notmatch '^[yY]$') { exit 0 }
$cli = Join-Path $app 'venv\Scripts\athlete-growth.exe'
if (Test-Path -LiteralPath $cli) {
  & $cli automation-uninstall-windows
  & $cli workbuddy-unconfigure
}
if (Test-Path -LiteralPath $app) {
  Remove-Item -LiteralPath $app -Recurse -Force
}
Write-Host '程序已移除。个人数据仍保留在用户目录的 .athlete-growth 文件夹。'
