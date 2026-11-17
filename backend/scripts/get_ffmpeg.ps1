# Tai FFmpeg (ffmpeg.exe + ffprobe.exe) vao backend\bin. Thu nhieu nguon, bao loi ro rang.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'          # tat thanh tien do cua PowerShell (lam tai cham rat nhieu)
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force $bin | Out-Null
if ((Test-Path "$bin\ffmpeg.exe") -and (Test-Path "$bin\ffprobe.exe")) { Write-Host 'FFmpeg da co san.'; exit 0 }
$urls = @(
  'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
  'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)
$tmp = Join-Path $env:TEMP ('ffmpeg_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $tmp | Out-Null
$ok = $false
foreach ($u in $urls) {
  try {
    Write-Host "Dang tai FFmpeg tu $u"
    $zip = Join-Path $tmp 'ffmpeg.zip'
    Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x') -Force
    Get-ChildItem (Join-Path $tmp 'x') -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $bin -Force
    if ((Test-Path "$bin\ffmpeg.exe") -and (Test-Path "$bin\ffprobe.exe")) { $ok = $true; break }
  } catch { Write-Host "Loi: $($_.Exception.Message)" }
}
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
if (-not $ok) { Write-Host 'KHONG tai duoc FFmpeg. Hay tai thu cong tai https://www.gyan.dev/ffmpeg/builds/ roi chep ffmpeg.exe va ffprobe.exe vao thu muc backend\bin'; exit 1 }
& "$bin\ffmpeg.exe" -version | Select-Object -First 1
exit 0
