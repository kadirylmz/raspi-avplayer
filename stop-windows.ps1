<#
 AV Player - Windows Kapatma / Durdurma / Yönetim Scripti
 
 Kullanım (Yönetici veya Standart PowerShell):
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1          # Oynatıcıyı ve servisleri durdurur
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Disable # Otomatik açılmayı geçici olarak kapatır
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Enable  # Otomatik açılmayı tekrar aktif eder
   powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Start   # Servisleri tekrar başlatır
#>
param(
  [switch]$Disable,
  [switch]$Enable,
  [switch]$Start
)

$ErrorActionPreference = "Continue"

function Run-Silent {
  param([scriptblock]$Script)
  $origEA = $global:ErrorActionPreference
  $global:ErrorActionPreference = "SilentlyContinue"
  try {
    & $Script 2>&1 | Out-Null
  } catch {}
  finally {
    $global:ErrorActionPreference = $origEA
  }
}

if ($Enable) {
  Write-Host "AVPlayer Windows otomatik başlangıç görevleri aktif ediliyor..." -ForegroundColor Cyan
  Run-Silent { schtasks /Change /TN "AVPlayer-Web" /Enable }
  Run-Silent { schtasks /Change /TN "AVPlayer-Player" /Enable }
  Write-Host "Aktif edildi. Bir sonraki oturum açılışında otomatik başlayacaktır." -ForegroundColor Green
  exit 0
}

if ($Start) {
  Write-Host "AVPlayer servisleri başlatılıyor..." -ForegroundColor Cyan
  Run-Silent { schtasks /Run /TN "AVPlayer-Web" }
  Run-Silent { schtasks /Run /TN "AVPlayer-Player" }
  Write-Host "Servisler başlatıldı. Web paneli ve oynatıcı devrede." -ForegroundColor Green
  exit 0
}

Write-Host "`n==> AV Player Windows servisleri ve oynatıcı kapatılıyor..." -ForegroundColor Yellow

# 1. Görev zamanlayıcı yürütmesini durdur
Run-Silent { schtasks /End /TN "AVPlayer-Web" }
Run-Silent { schtasks /End /TN "AVPlayer-Player" }

if ($Disable) {
  Write-Host "  Otomatik açılış (ONLOGON) devre dışı bırakılıyor..." -ForegroundColor DarkYellow
  Run-Silent { schtasks /Change /TN "AVPlayer-Web" /Disable }
  Run-Silent { schtasks /Change /TN "AVPlayer-Player" /Disable }
}

# 2. MPV Oynatıcısını kapat
$mpvProcs = Get-Process -Name "mpv" -ErrorAction SilentlyContinue
if ($mpvProcs) {
  Write-Host "  MPV oynatıcı kapatılıyor ($($mpvProcs.Count) işlem)..." -ForegroundColor Gray
  $mpvProcs | Stop-Process -Force -ErrorAction SilentlyContinue
}

# 3. Kiosk Modunda çalışan özel EXE veya Python işlemlerini sonlandır
# AVPlayer'a ait python process'lerini tespit et
$pyProcs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue | Where-Object {
  $_.CommandLine -like "*app.main:app*" -or $_.CommandLine -like "*app.player_daemon*"
}
foreach ($p in $pyProcs) {
  Write-Host "  AVPlayer arka plan servisi sonlandırılıyor (PID $($p.ProcessId))..." -ForegroundColor Gray
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 4. Kiosk EXE uygulaması varsa (C:\ProgramData\AVPlayer\kiosk\ içinde çalışan)
$kioskProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
  $_.ExecutablePath -like "C:\ProgramData\AVPlayer\kiosk\*"
}
foreach ($kp in $kioskProcs) {
  Write-Host "  Kiosk uygulaması sonlandırılıyor ($($kp.Name) - PID $($kp.ProcessId))..." -ForegroundColor Gray
  Stop-Process -Id $kp.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "`n[OK] AV Player ve Oynatıcı tamamen durduruldu." -ForegroundColor Green
if ($Disable) {
  Write-Host "     Otomatik başlangıç kapatıldı. Tekrar açmak için: .\stop-windows.ps1 -Enable" -ForegroundColor DarkCyan
} else {
  Write-Host "     Tekrar başlatmak için: powershell -ExecutionPolicy Bypass -File .\stop-windows.ps1 -Start" -ForegroundColor DarkCyan
}
