<#
 AV Player - Windows Kapatma / Durdurma / Yönetim Scripti
 
 Kullanım (Yönetici veya Standart PowerShell):
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1          # Oynatıcıyı, watchdog'u ve servisleri TAMAMEN durdurur
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Disable # Otomatik açılmayı kalıcı kapatır
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Enable  # Otomatik açılmayı tekrar aktif eder
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Start   # Servisleri tekrar başlatır
#>
param(
  [switch]$Disable,
  [switch]$Enable,
  [switch]$Start
)

$ErrorActionPreference = "SilentlyContinue"

function Run-Silent {
  param([scriptblock]$Script)
  try { & $Script 2>&1 | Out-Null } catch {}
}

$dataDir = "C:\ProgramData\AVPlayer"
$stopFlag = "$dataDir\stop.flag"

if ($Enable) {
  Write-Host "AVPlayer Windows otomatik başlangıç görevleri aktif ediliyor..." -ForegroundColor Cyan
  Remove-Item $stopFlag -Force -ErrorAction SilentlyContinue
  Run-Silent { schtasks /Change /TN "AVPlayer-Web" /Enable }
  Run-Silent { schtasks /Change /TN "AVPlayer-Player" /Enable }
  Write-Host "Aktif edildi. Oturum açılışında otomatik başlayacaktır." -ForegroundColor Green
  exit 0
}

if ($Start) {
  Write-Host "AVPlayer servisleri başlatılıyor..." -ForegroundColor Cyan
  Remove-Item $stopFlag -Force -ErrorAction SilentlyContinue
  Run-Silent { schtasks /Change /TN "AVPlayer-Web" /Enable }
  Run-Silent { schtasks /Change /TN "AVPlayer-Player" /Enable }
  Run-Silent { schtasks /Run /TN "AVPlayer-Web" }
  Run-Silent { schtasks /Run /TN "AVPlayer-Player" }
  Write-Host "Servisler başlatıldı. Web paneli ve oynatıcı devrede." -ForegroundColor Green
  exit 0
}

Write-Host "`n==> AV Player ve Oynatıcı Kökten Kapatılıyor..." -ForegroundColor Yellow

# 0. Watchdog döngülerinin anında çıkması için stop bayrağı bırak
if (Test-Path $dataDir) {
  Set-Content -Path $stopFlag -Value "STOP" -Force -ErrorAction SilentlyContinue
}

# 1. Görev zamanlayıcı yürütmesini durdur ve geçici olarak pasife al (tekrar hortlamasın)
Run-Silent { schtasks /End /TN "AVPlayer-Web" }
Run-Silent { schtasks /End /TN "AVPlayer-Player" }
Run-Silent { schtasks /Change /TN "AVPlayer-Web" /Disable }
Run-Silent { schtasks /Change /TN "AVPlayer-Player" /Disable }

# 2. run-player.ps1 veya run-web.ps1 çalıştıran TÜM arka plan PowerShell watchdog süreçlerini öldür
$psProcs = Get-CimInstance Win32_Process -Filter "Name LIKE 'powershell%' OR Name LIKE 'pwsh%'" -ErrorAction SilentlyContinue | Where-Object {
  $_.CommandLine -like "*run-player.ps1*" -or $_.CommandLine -like "*run-web.ps1*"
}
foreach ($p in $psProcs) {
  Write-Host "  Watchdog döngüsü sonlandırılıyor (PID $($p.ProcessId))..." -ForegroundColor Gray
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 3. MPV Oynatıcısını anında kapat
$mpvProcs = Get-Process -Name "mpv" -ErrorAction SilentlyContinue
if ($mpvProcs) {
  Write-Host "  MPV oynatıcı kapatılıyor ($($mpvProcs.Count) işlem)..." -ForegroundColor Gray
  $mpvProcs | Stop-Process -Force -ErrorAction SilentlyContinue
}

# 4. AVPlayer Python süreçlerini kapat
$pyProcs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue | Where-Object {
  $_.CommandLine -like "*app.main:app*" -or $_.CommandLine -like "*app.player_daemon*"
}
foreach ($p in $pyProcs) {
  Write-Host "  AVPlayer python servisi sonlandırılıyor (PID $($p.ProcessId))..." -ForegroundColor Gray
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 5. Kiosk EXE uygulaması varsa kapat
$kioskProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
  $_.ExecutablePath -like "C:\ProgramData\AVPlayer\kiosk\*"
}
foreach ($kp in $kioskProcs) {
  Write-Host "  Kiosk uygulaması sonlandırılıyor ($($kp.Name) - PID $($kp.ProcessId))..." -ForegroundColor Gray
  Stop-Process -Id $kp.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "`n[OK] Sistem ve Karşılama Ekranı tamamen kapatıldı." -ForegroundColor Green
Write-Host "     Tekrar başlatmak istediğinizde: powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Start`n" -ForegroundColor DarkCyan
