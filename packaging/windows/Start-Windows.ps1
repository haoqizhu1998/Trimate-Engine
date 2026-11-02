$ErrorActionPreference = 'Stop'
$app = Join-Path $env:LOCALAPPDATA 'AthleteGrowth'
$cli = Join-Path $app 'venv\Scripts\athlete-growth.exe'
if (-not (Test-Path -LiteralPath $cli)) { throw '请先双击 Install-Windows.cmd 安装。' }

& $cli status *> $null
if ($LASTEXITCODE -ne 0) {
  $name = Read-Host 'Trimate怎样称呼你？（直接回车使用“运动员”）'
  if (-not $name) { $name = '运动员' }
  $timezone = Read-Host '时区（直接回车使用 Asia/Shanghai）'
  if (-not $timezone) { $timezone = 'Asia/Shanghai' }
  & $cli init --name $name --timezone $timezone --units metric
  if ($LASTEXITCODE -ne 0) { throw '建立本地档案失败。' }
}
Write-Host '个人档案已就绪。'
$autoSelected = $false
$garmin = Read-Host '现在连接佳明中国区账号？(y/N)'
if ($garmin -match '^[yY]$') {
  & $cli garmin-cn-login
  if ($LASTEXITCODE -eq 0) {
    & $cli automation-configure --source garmin-cn
    if ($LASTEXITCODE -eq 0) { $autoSelected = $true }
    & $cli automation-run-once --force
  } else {
    Write-Warning '佳明连接未完成，之后可重新打开本引导。'
  }
}

$coros = Read-Host '现在连接高驰中国区账号？(y/N)'
if ($coros -match '^[yY]$') {
  & $cli coros-helper-install
  if ($LASTEXITCODE -ne 0) { throw '无法下载官方高驰登录工具。' }
  & $cli coros-login-start
  if ($LASTEXITCODE -eq 0) {
    Read-Host '在上方官方链接完成授权后，按回车继续' | Out-Null
    & $cli coros-login-finish
    if ($LASTEXITCODE -eq 0) {
      $start = (Get-Date).AddDays(-2).ToString('yyyy-MM-dd')
      $end = (Get-Date).ToString('yyyy-MM-dd')
      & $cli sync --source coros --start $start --end $end
      if ($LASTEXITCODE -eq 0) {
        & $cli automation-configure --source coros
        if ($LASTEXITCODE -eq 0) { $autoSelected = $true }
      } else {
        Write-Warning '高驰同步未通过，自动同步暂不启用。'
      }
    }
  }
}

if ($autoSelected) {
  $answer = Read-Host '电脑登录时自动同步并分析训练数据？(y/N)'
  if ($answer -match '^[yY]$') {
    & $cli automation-install-windows
    if ($LASTEXITCODE -ne 0) {
      Write-Warning 'Windows 自动任务安装失败，仍可在本机手动同步。'
    }
  }
}
Write-Host '请回到当前 AI 对话，询问：检查我的训练数据，帮我开始使用。'
