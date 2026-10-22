<#
.SYNOPSIS
  Install or update IEUStudia for the current user (no admin rights needed).

.DESCRIPTION
  Builds the app from this repository and installs it into
  %LOCALAPPDATA%\Programs\IEUStudia with its own Python environment, then creates
  "IEUStudia" shortcuts on the Desktop and in the Start menu.
  Re-run it after changing the code to update the installed copy. Your data (settings,
  library, transcripts, models) lives in %LOCALAPPDATA%\IEUStudia and is never touched.
#>
param(
    [string]$Dest = (Join-Path $env:LOCALAPPDATA "Programs\IEUStudia"),
    [switch]$NoShortcuts
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$appId = "Studia.App"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Host "uv (the Python package manager) is required: https://docs.astral.sh/uv/getting-started/installation/" -ForegroundColor Yellow
    exit 1
}

# Stop a running copy (also of the app's earlier versions, Studia and LectureScribe) so its files can be replaced.
$legacy = Join-Path $env:LOCALAPPDATA "Programs\LectureScribe"
$previous = Join-Path $env:LOCALAPPDATA "Programs\Studia"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='studia-gui.exe' OR Name='lecturescribe-gui.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$Dest*" -or $_.CommandLine -like "*$legacy*" -or $_.CommandLine -like "*$previous\*") } |
    ForEach-Object { Step "Closing the running app (pid $($_.ProcessId))"; Stop-Process -Id $_.ProcessId -Force }

Step "Building the app"
$build = Join-Path $env:TEMP "studia-build"
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
Push-Location $repo
try { & $uv build --wheel --out-dir $build | Out-Host } finally { Pop-Location }
$wheel = Get-ChildItem $build -Filter "studia-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "Build failed: no wheel produced." }

Step "Installing into $Dest"
New-Item -ItemType Directory -Force $Dest | Out-Null
$py = Join-Path $Dest ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { & $uv venv --python 3.12 (Join-Path $Dest ".venv") | Out-Host }
& $uv pip install --python $py --compile-bytecode --reinstall-package studia $wheel.FullName | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Installing the app failed." }
Copy-Item (Join-Path $repo "src\studia\assets\icon.ico") (Join-Path $Dest "IEUStudia.ico") -Force
Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
$target = Join-Path $Dest ".venv\Scripts\studia-gui.exe"
if (-not (Test-Path $target)) { throw "Launcher not found: $target" }

if (-not $NoShortcuts) {
    Step "Creating shortcuts"
    Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore { int GetCount(out uint c); int GetAt(uint i, out PropertyKey k); int GetValue(ref PropertyKey k, out PropVariant v);
  int SetValue(ref PropertyKey k, ref PropVariant v); int Commit(); }
[StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PropertyKey { public Guid fmtid; public uint pid; }
[StructLayout(LayoutKind.Explicit)] public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
public static class LnkAppId {
  [DllImport("shell32.dll")] static extern int SHGetPropertyStoreFromParsingName([MarshalAs(UnmanagedType.LPWStr)] string path,
     IntPtr bc, int flags, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
  public static void Set(string lnk, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"); IPropertyStore s;
    Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out s));
    var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    var v = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(id) };
    Marshal.ThrowExceptionForHR(s.SetValue(ref key, ref v)); Marshal.ThrowExceptionForHR(s.Commit());
    Marshal.FreeCoTaskMem(v.p); Marshal.ReleaseComObject(s); } }
"@ -ErrorAction SilentlyContinue
    $ws = New-Object -ComObject WScript.Shell
    $links = @(
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "IEUStudia.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Programs")) "IEUStudia.lnk")
    )
    foreach ($lnk in $links) {
        $s = $ws.CreateShortcut($lnk)
        $s.TargetPath = $target
        $s.WorkingDirectory = $Dest
        $s.IconLocation = (Join-Path $Dest "IEUStudia.ico") + ",0"
        $s.Description = "Your courses, lectures, materials and AI study tools"
        $s.Save()
        try { [LnkAppId]::Set($lnk, $appId) } catch { Write-Host "   (could not set taskbar id on $lnk)" -ForegroundColor DarkGray }
        Write-Host "   $lnk"
    }
}

# The app used to be called LectureScribe, then Studia: remove those program folders and shortcuts. The data is
# not deleted: the app moves a data folder of those names to %LOCALAPPDATA%\IEUStudia on its first start.
foreach ($old in $legacy, $previous) {
    if ((Test-Path $old) -and ($old -ne $Dest)) {
        Step "Removing the old program in $old"
        Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue
    }
}
foreach ($name in "LectureScribe.lnk", "Studia.lnk") {
    foreach ($old in @((Join-Path ([Environment]::GetFolderPath("Desktop")) $name),
                       (Join-Path ([Environment]::GetFolderPath("Programs")) $name))) {
        if (Test-Path $old) { Remove-Item -Force $old -ErrorAction SilentlyContinue }
    }
}

Write-Host ""
Write-Host "IEUStudia is installed. Start it from the Desktop or Start menu." -ForegroundColor Green
Write-Host "Program: $Dest"
Write-Host "Data:    $(Join-Path $env:LOCALAPPDATA 'IEUStudia')"
