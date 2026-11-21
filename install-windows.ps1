<#
 AV Player - Windows Kiosk / Media Server Kurulum Scripti (Windows 10 / 11)
 Birebir Raspberry Pi mimarisiyle aynı Web Paneli, Karşılama Ekranı, FFmpeg Dönüştürme ve 60sn Reset sistemini Windows PC'ye kurar.

 Kullanım (Yönetici PowerShell):
   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Port 80 -StaticIP 192.168.1.220
   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall
#>
param(
  [int]$Port = 80,
  [string]$DataDir = "C:\ProgramData\AVPlayer",
  [string]$StaticIP = "",
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
  Write-Host "AV Player Windows görevleri kaldırılıyor..." -ForegroundColor Yellow
  schtasks /End /TN "AVPlayer-Web" 2>$null | Out-Null
  schtasks /End /TN "AVPlayer-Player" 2>$null | Out-Null
  schtasks /Delete /TN "AVPlayer-Web" /F 2>$null | Out-Null
  schtasks /Delete /TN "AVPlayer-Player" /F 2>$null | Out-Null
  Remove-NetFirewallRule -DisplayName "AVPlayer-Web" -ErrorAction SilentlyContinue
  Write-Host "Kaldırıldı. (Veri klasörü korundu: $DataDir)" -ForegroundColor Green
  exit 0
}

Write-Host "`n==> [1/5] Python, FFmpeg ve MPV kontrol ediliyor..." -ForegroundColor Cyan
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "Python 3.11+ bulunamadı. Lütfen python.org veya Microsoft Store üzerinden Python kurun." }

foreach ($tool in @(
  @{ cmd = "ffmpeg"; winget = "Gyan.FFmpeg" },
  @{ cmd = "mpv";    winget = "mpv.net" }
)) {
  if (-not (Get-Command $tool.cmd -ErrorAction SilentlyContinue)) {
    Write-Host "  $($tool.cmd) PATH üzerinde bulunamadı, winget ile kuruluyor..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      if ($tool.cmd -eq "mpv") {
        winget install --id=shinchiro.mpv -e --accept-source-agreements --accept-package-agreements 2>$null
      } else {
        winget install --id=$($tool.winget) -e --accept-source-agreements --accept-package-agreements 2>$null
      }
    }
  }
}

Write-Host "`n==> [2/5] Python paketleri yükleniyor (FastAPI, Uvicorn, Pillow, psutil, httpx)..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip -q
& $py -m pip install -r "$here\requirements.txt" static-ffmpeg -q

Write-Host "`n==> [3/5] Veri klasörü ve Başlatıcı (Watchdog) scriptleri hazırlanıyor ($DataDir)..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

$webRunner = "$DataDir\run-web.ps1"
@"
`$env:AVP_DATA = "$DataDir"
`$env:AVP_RUN  = "$DataDir\run"
`$env:AVP_PORT = "$Port"
Set-Location "$here"
while (`$true) {
  & "$py" -c "try:`n import static_ffmpeg; static_ffmpeg.add_paths()`nexcept Exception: pass`nimport uvicorn; uvicorn.run('app.main:app', host='0.0.0.0', port=$Port, access_log=False)"
  Start-Sleep -Seconds 2
}
"@ | Set-Content -Path $webRunner -Encoding UTF8

$playerRunner = "$DataDir\run-player.ps1"
@"
`$env:AVP_DATA = "$DataDir"
`$env:AVP_RUN  = "$DataDir\run"
Set-Location "$here"
while (`$true) {
  & "$py" -c "try:`n import static_ffmpeg; static_ffmpeg.add_paths()`nexcept Exception: pass`nfrom app.player_daemon import main; main()"
  Start-Sleep -Seconds 2
}
"@ | Set-Content -Path $playerRunner -Encoding UTF8

Write-Host "`n==> [4/5] Windows Kiosk Güç Ayarları, Güvenlik Duvarı ve Otomatik Başlatma..." -ForegroundColor Cyan
# Uyku ve ekran kapatmayı devre dışı bırak (7/24 müze/sergi kullanımı)
powercfg /change monitor-timeout-ac 0 2>$null
powercfg /change standby-timeout-ac 0 2>$null
powercfg /change hibernate-timeout-ac 0 2>$null

# Güvenlik duvarında Web Paneli portunu aç
Remove-NetFirewallRule -DisplayName "AVPlayer-Web" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "AVPlayer-Web" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null

# Windows açıldığında / kullanıcı oturum açtığında otomatik başlayan Görev Zamanlayıcı (Scheduled Tasks)
schtasks /Delete /TN "AVPlayer-Web" /F 2>$null | Out-Null
schtasks /Delete /TN "AVPlayer-Player" /F 2>$null | Out-Null
schtasks /Create /TN "AVPlayer-Web" /TR "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$webRunner`"" /SC ONLOGON /RL HIGHEST /F | Out-Null
schtasks /Create /TN "AVPlayer-Player" /TR "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$playerRunner`"" /SC ONLOGON /RL HIGHEST /F | Out-Null

if ($StaticIP) {
  Write-Host "`n==> [Opsiyonel] Windows Sabit IP ayarlanıyor ($StaticIP)..." -ForegroundColor Cyan
  $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Sort-Object RouteMetric | Select-Object -First 1
  if ($route) {
    $ifIdx = $route.InterfaceIndex
    $gw = $route.NextHop
    $cur = Get-NetIPAddress -InterfaceIndex $ifIdx -AddressFamily IPv4 | Select-Object -First 1
    $prefix = if ($cur) { $cur.PrefixLength } else { 24 }
    Set-NetIPInterface -InterfaceIndex $ifIdx -Dhcp Disabled -ErrorAction SilentlyContinue
    Remove-NetIPAddress -InterfaceIndex $ifIdx -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $ifIdx -IPAddress $StaticIP -PrefixLength $prefix -DefaultGateway $gw | Out-Null
    Set-DnsClientServerAddress -InterfaceIndex $ifIdx -ServerAddresses $gw | Out-Null
  }
}

Write-Host "`n==> [5/5] Servisler başlatılıyor..." -ForegroundColor Cyan
schtasks /Run /TN "AVPlayer-Web" | Out-Null
schtasks /Run /TN "AVPlayer-Player" | Out-Null
Start-Sleep -Seconds 3

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
$url = if ($Port -eq 80) { "http://$($ip)" } else { "http://$($ip):$Port" }
Write-Host "`nKurulum Tamamlandı!" -ForegroundColor Green
Write-Host "  Web Paneli:  $url  (veya http://localhost:$(if($Port -eq 80){80}else{$Port}))" -ForegroundColor Green
Write-Host "  Veri Dizini: $DataDir"
Start-Process "http://localhost:$Port"
