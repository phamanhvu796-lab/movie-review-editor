; Inno Setup 6 - tao MovieReviewEditor-Setup-<phien ban>.exe (cai theo tung nguoi dung, KHONG can quyen Admin)
; CAI MOT CHAM: bam doi la cai vao %LOCALAPPDATA%\Programs\MovieReviewEditor, tao icon Desktop va mo app ngay.
; Cap nhat online: app chay file nay voi /VERYSILENT /UPDATE=1 -> cai de len ban cu (giu du lieu) roi tu mo lai app.
; Bien dich: ISCC.exe /DMyAppVersion=1.0.0 installer\MovieReviewEditor.iss   (build_installer.bat va GitHub Actions da lam san)
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Movie Review Editor"
#define MyAppExe "MovieReviewEditor.exe"

[Setup]
AppId={{B7E2C5A1-3F4D-4C9E-9A55-6D1E2F0A7C11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Movie Review Editor
DefaultDirName={autopf}\MovieReviewEditor
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=MovieReviewEditor-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
DisableWelcomePage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
UsePreviousAppDir=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExe}
CloseApplications=force
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[InstallDelete]
; Cap nhat: xoa thu vien cua ban cu truoc khi chep ban moi (khong tron 2 phien ban). Du lieu nguoi dung o noi khac, khong bi dong.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\backend\dist\MovieReviewEditor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
#ifexist "..\backend\vc_redist.x64.exe"
Source: "..\backend\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif
#ifexist "..\backend\MicrosoftEdgeWebview2Setup.exe"
Source: "..\backend\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"

[Run]
#ifexist "..\backend\vc_redist.x64.exe"
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Dang cai Microsoft Visual C++ Runtime (chi can cho may chua co)..."; Check: NeedsVCRedist; Verb: runas; Flags: shellexec waituntilterminated
#endif
#ifexist "..\backend\MicrosoftEdgeWebview2Setup.exe"
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Dang cai Microsoft Edge WebView2 (cua so ung dung)..."; Check: NeedsWebView2; Flags: waituntilterminated
#endif
; Luon mo app sau khi cai (ca khi cai mot cham lan dau lan khi cap nhat im lang tu trong app)
Filename: "{app}\{#MyAppExe}"; Flags: nowait

[Code]
function NeedsVCRedist: Boolean;
var
  Installed: Cardinal;
begin
  Result := not (RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1));
end;

function NeedsWebView2: Boolean;
var
  V: String;
begin
  Result := not ((RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0'))
    or (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0')));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{%USERPROFILE}') + '\MovieReviewEditor';
    if DirExists(DataDir) then
      if MsgBox('Xoa luon du lieu (du an, video da xuat, key API, key bao mat) trong ' + DataDir + ' ?' + #13#10 + 'Chon No de giu lai.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
