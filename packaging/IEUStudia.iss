; Inno Setup script for the IEUStudia installer (per-user, no admin rights).
;   1. build the app:  uv run pyinstaller packaging\IEUStudia.spec --noconfirm --distpath build\dist --workpath build\work
;   2. build this:     ISCC.exe /DAppVersion=2.1.0 packaging\IEUStudia.iss
; or run scripts\build_release.ps1, which does both. The installer lands in build\installer\.

#define AppName "IEUStudia"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com/mirzaisi/IEUStudia"
#endif

[Setup]
AppId={{8E1B7D57-5C2A-4F07-9E33-3C9A1D2B7F11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=IEUStudia contributors
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}
DefaultDirName={localappdata}\Programs\IEUStudia
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\build\installer
OutputBaseFilename=IEUStudia-Setup-{#AppVersion}
SetupIconFile=..\src\studia\assets\icon.ico
UninstallDisplayIcon={app}\IEUStudia.exe
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\build\dist\IEUStudia\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files of an older version that a newer build no longer has
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\IEUStudia"; Filename: "{app}\IEUStudia.exe"; AppUserModelID: "Studia.App"
Name: "{autodesktop}\IEUStudia"; Filename: "{app}\IEUStudia.exe"; Tasks: desktopicon; AppUserModelID: "Studia.App"

[Run]
Filename: "{app}\IEUStudia.exe"; Description: "{cm:LaunchProgram,IEUStudia}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /f /im IEUStudia.exe"; Flags: runhidden; RunOnceId: "StopIEUStudia"

[Code]
function WebView2Installed(): Boolean;
var
  v: String;
begin
  Result := RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
var
  code: Integer;
begin
  Result := True;
  if not WebView2Installed() then
    if MsgBox('IEUStudia needs the Microsoft Edge WebView2 Runtime, which was not found on this PC.' + #13#10#13#10 +
              'It is free and comes with Windows 11 and current Windows 10. Open the download page now? ' +
              '(You can continue the installation either way.)', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/#download', '', '', SW_SHOWNORMAL, ewNoWait, code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  data: String;
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'IEUStudia');
  if CurUninstallStep = usPostUninstall then
  begin
    data := ExpandConstant('{localappdata}\Studia');
    if DirExists(data) and not UninstallSilent() then
      if MsgBox('Also delete IEUStudia''s data on this PC?' + #13#10#13#10 +
                'This removes your settings, the stored (encrypted) IEULearn login, the library and the downloaded ' +
                'models in ' + data + '. Your transcripts and notes in your Documents folder are kept.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(data, True, True, True);
  end;
end;
