param([switch]$Automated, [ValidateSet('codex', 'workbuddy')][string]$HostName)
$ErrorActionPreference = 'Stop'
if ($Automated -and -not $HostName) { throw '自动安装须指定 codex 或 workbuddy 宿主。' }
$bundle = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $env:LOCALAPPDATA 'AthleteGrowth'
$venv = Join-Path $app 'venv'
$wheel = Get-ChildItem -LiteralPath $bundle -Filter 'athlete_growth-*.whl' -File | Select-Object -First 1
if (-not $wheel) { throw '安装包不完整：找不到产品文件。' }
New-Item -ItemType Directory -Path $app -Force | Out-Null
Write-Host '正在安装Trimate。首次安装需要联网下载 Python 环境和依赖。'

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
  $uv = $uvCommand.Source
} else {
  $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
  if (-not (Test-Path -LiteralPath $uv)) {
    $installer = Join-Path $app 'uv-install.ps1'
    Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer -UseBasicParsing
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
    if ($LASTEXITCODE -ne 0) { throw 'Python 环境安装器未完成。' }
  }
}
if (-not (Test-Path -LiteralPath $uv)) { throw '找不到 uv，请检查网络后重试。' }

$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & $uv venv --python 3.12 $venv
  if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
}
& $uv pip install --reinstall-package athlete-growth --python $python $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw '安装产品依赖失败。' }

$cli = Join-Path $venv 'Scripts\athlete-growth.exe'
if (-not (Test-Path -LiteralPath $cli)) { throw '安装未完成：找不到产品入口。' }
Copy-Item -LiteralPath (Join-Path $bundle 'Start-Windows.ps1') -Destination $app -Force
Copy-Item -LiteralPath (Join-Path $bundle 'Start-Windows.cmd') -Destination $app -Force
Copy-Item -LiteralPath (Join-Path $bundle 'Uninstall-Windows.ps1') -Destination $app -Force
Copy-Item -LiteralPath (Join-Path $bundle 'Uninstall-Windows.cmd') -Destination $app -Force
if ($Automated) {
  & $python (Join-Path $bundle 'bootstrap-local.py') --host $HostName --cli $cli
  if ($LASTEXITCODE -ne 0) { throw '本地档案或 MCP 配置未完成。' }
  exit 0
}
Write-Host '程序安装完成，接下来建立个人档案。'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'Start-Windows.ps1')
if ($LASTEXITCODE -ne 0) { throw '个人档案引导未完成，可重新双击 Start-Windows.cmd。' }
Write-Host '请回到当前 AI 对话，让 AI 检查并连接本机 Trimate MCP。'
