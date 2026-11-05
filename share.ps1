# TRPG 地图工坊 · 公网分享启动器
# 作用：开启 Cloudflare 免费隧道，得到 https://xxx.trycloudflare.com 链接，
#       再启动本地服务器（含房间实时同步）。关闭窗口即停止分享。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cf = 'D:\tools_space_ai\cloudflared.exe'
$port = 8000

Write-Host ''
Write-Host '  ============================================================' -ForegroundColor Cyan
Write-Host '    TRPG 地图工坊 · 正在开启公网分享，请稍等 5~15 秒...' -ForegroundColor Cyan
Write-Host '  ============================================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $cf)) {
  Write-Host "  找不到 $cf" -ForegroundColor Red
  Write-Host '  可改用「启动网站（局域网）.bat」，或把 cloudflared.exe 放回该目录。' -ForegroundColor Yellow
  Start-Sleep -Seconds 4
  exit 1
}

# 清掉旧隧道进程
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$log = Join-Path $env:TEMP 'trpg_tunnel.log'
Remove-Item $log -ErrorAction SilentlyContinue

$tunnel = Start-Process -FilePath $cf `
  -ArgumentList @('tunnel', '--url', "http://127.0.0.1:$port", '--logfile', $log, '--loglevel', 'info', '--no-autoupdate') `
  -PassThru -WindowStyle Hidden

$public = $null
for ($i = 0; $i -lt 80; $i++) {
  Start-Sleep -Milliseconds 500
  if (Test-Path $log) {
    $text = Get-Content $log -Raw -ErrorAction SilentlyContinue
    if ($text -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
      $public = $Matches[0]
      break
    }
  }
  if ($tunnel.HasExited) { break }
}

if ($public) {
  try { Set-Clipboard -Value $public } catch {}
  Write-Host '  ============================================================' -ForegroundColor Green
  Write-Host '    公网分享链接（已复制到剪贴板，直接粘贴给朋友）:' -ForegroundColor Green
  Write-Host "      $public" -ForegroundColor White
  Write-Host '    任何网络的人打开这个链接，就是同一个房间、同一张地图' -ForegroundColor DarkGray
  Write-Host '  ============================================================' -ForegroundColor Green
  Write-Host ''
} else {
  Write-Host '  公网隧道没能建立（可能是网络限制）。' -ForegroundColor Yellow
  Write-Host '  本次仍可用「启动网站（局域网）.bat」在同一 WiFi 内使用。' -ForegroundColor Yellow
  Write-Host ''
}

$srvArgs = @('server.py', '--port', $port, '--open')
if ($public) { $srvArgs += @('--public-url', $public) }

$srv = Start-Process -FilePath 'python' -ArgumentList $srvArgs -WorkingDirectory $here -PassThru -NoNewWindow

try {
  Wait-Process -Id $srv.Id
} finally {
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  if ($public) {
    Write-Host ''
    Write-Host '  公网分享已停止（上面的链接已失效）。' -ForegroundColor DarkGray
  }
}
