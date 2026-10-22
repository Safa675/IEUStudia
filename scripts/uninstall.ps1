<#
.SYNOPSIS
  Remove the installed IEUStudia program and its shortcuts.
  -Purge also deletes app data (settings, stored login, library index, downloaded models).
  Your transcript files in the output folder (Documents\Studia by default) are never deleted.
#>
param(
    [string]$Dest = (Join-Path $env:LOCALAPPDATA "Programs\IEUStudia"),
    [switch]$Purge
)
$ErrorActionPreference = "Stop"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='studia-gui.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Dest*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
foreach ($name in "IEUStudia.lnk", "Studia.lnk") {
    foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath("Desktop")) $name),
                       (Join-Path ([Environment]::GetFolderPath("Programs")) $name))) {
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed $lnk" }
    }
}
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest; Write-Host "Removed $Dest" }
if ($Purge) {
    $data = Join-Path $env:LOCALAPPDATA "Studia"
    if (Test-Path $data) { Remove-Item -Recurse -Force $data; Write-Host "Removed $data" }
}
Write-Host "IEUStudia was uninstalled." -ForegroundColor Green
