<#
.SYNOPSIS
  Remove the installed IEUStudia program, its shortcuts and its start-with-Windows entry.
  -Purge also deletes app data (settings, stored login, library index, downloaded models).
  Your transcript files in the output folder (Documents\IEUStudia by default) are never deleted.
#>
param(
    [string]$Dest = (Join-Path $env:LOCALAPPDATA "Programs\IEUStudia"),
    [switch]$Purge
)
$ErrorActionPreference = "Stop"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='studia-gui.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Dest*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
foreach ($name in "IEUStudia.lnk", "Studia.lnk", "LectureScribe.lnk") {
    foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath("Desktop")) $name),
                       (Join-Path ([Environment]::GetFolderPath("Programs")) $name))) {
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed $lnk" }
    }
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "IEUStudia" -ErrorAction SilentlyContinue
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest; Write-Host "Removed $Dest" }
if ($Purge) {
    # the data folder, also under the app's earlier names (Studia, LectureScribe) if it was never moved
    foreach ($name in "IEUStudia", "Studia", "LectureScribe") {
        $data = Join-Path $env:LOCALAPPDATA $name
        if ((Test-Path $data) -and ($name -eq "IEUStudia" -or (Test-Path (Join-Path $data "library.db")))) {
            Remove-Item -Recurse -Force $data; Write-Host "Removed $data"
        }
    }
}
Write-Host "IEUStudia was uninstalled." -ForegroundColor Green
