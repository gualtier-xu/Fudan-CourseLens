; CourseLens per-user installer shell (PKG-MIGRATION-1, from N5K §38.1/§39.2/§38.5).
; Build: installer\build_installer.ps1  (requires Inno Setup 6.3+ ISCC)
; The AppId must never change after the first public release; upgrades match on it.
; The two install roots are pinned per 卡③: payload = {localappdata}\Programs\CourseLens,
; managed data root = {localappdata}\CourseLens (never touched by uninstall by default).

#define AppName "CourseLens"
#ifndef AppVersion
#define AppVersion "0.1.0"
#endif
#ifndef SourceRoot
#define SourceRoot "payload"
#endif

[Setup]
AppId={{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\CourseLens
; P66 INSTALLER-UX-1: the destination page must always show (the default
; "auto" hides it because the pinned default sits under {localappdata}).
; The choice moves the payload shell ({app}) only: the managed data root
; stays pinned at {localappdata}\CourseLens (卡③ two-root layout; upgrade
; continuity + the uninstall data red line), and the dir page says so.
DisableDirPage=no
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
Uninstallable=yes
OutputBaseFilename=CourseLens-{#AppVersion}-setup
ShowLanguageDialog=no

; N9-G INSTALLER-POLISH-1 U4: Explorer-visible identity. SetupIconFile brands
; both setup.exe and unins000.exe (Inno 6.2.2+); the App* metadata and the
; VersionInfo* family fill the Explorer properties page. The icon is the
; product's own book.svg mark rasterized to six sizes (16-256).
SetupIconFile=courselens-icon.ico
; INSTALLER-ICON (0.1.1): the uninstall entry must carry the product icon
; too — without UninstallDisplayIcon Inno writes no DisplayIcon value and
; 系统 设置→应用 shows the generic icon (0.1.0 defect, seen on the live
; install). Resolves through the payload copy already shipped by [Files],
; like every shortcut icon below.
UninstallDisplayIcon={app}\courselens-icon.ico
AppPublisher=CourseLens
AppPublisherURL=https://github.com/gualtier-xu/Fudan-CourseLens
AppUpdatesURL=https://github.com/gualtier-xu/Fudan-CourseLens
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=CourseLens
VersionInfoDescription=CourseLens 安装程序（复旦学习助手）
VersionInfoTextVersion={#AppVersion}

; Simplified Chinese is vendored in this directory (official Inno installers do
; not bundle it); the script-relative reference needs no <Inno> setup step.
[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"

; P66 INSTALLER-UX-1: tell students where their data lives before they pick a
; folder — the page moves the program only; the data root is separate and the
; uninstall prompt keeps it by default (数据保护红线, restated in student words).
[Messages]
SelectDirLabel3=选择 CourseLens 程序文件的安装位置。%n%n你的学习数据（笔记、进度、登录状态）不在这里：它们始终存放在本机的 CourseLens 数据目录，卸载时默认为你完整保留。

[Files]
; CO-NEUTRAL-2: the checkout-only ops sidecar and the private-channel exporter
; are repository tooling, not student payload - the private repository name
; they carry must never ride the staging tree into setup.exe (zero co inside
; the installer; REBUILD-9C finding, fixed here).
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Excludes: "config\ops-private.json,scripts\export_worker_mirror.py"; Flags: recursesubdirs createallsubdirs
; P66 INSTALLER-UX-1: the product icon travels with the payload shell so every
; shortcut can reference it via {app} (script-relative, like SetupIconFile).
; The -darktaskbar sibling is the tray's dark-taskbar variant (SYSTRAY-IMPL-3):
; tray_manager.py picks it by AppsUseLightTheme next to whichever icon resolves.
Source: "courselens-icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "courselens-icon-darktaskbar.ico"; DestDir: "{app}"; Flags: ignoreversion

; P66 INSTALLER-UX-1: the desktop shortcut is checked by default (the
; double-click entry students expect); the quick-launch pin stays optional.
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
; All shortcuts target the pythonw silent host inside the managed root
; (generated at [Run] time by install_managed_client.ps1): no console window
; ever exists, and every lnk carries the product icon. -B disables bytecode
; at interpreter startup (before any script line runs): a bare pythonw would
; write __pycache__ files into the trust-verified launcher tree and the
; trust walk would refuse the launch.
Name: "{autoprograms}\{#AppName}"; Filename: "{localappdata}\CourseLens\launcher\python312\pythonw.exe"; Parameters: "-B ""{localappdata}\CourseLens\launcher\start_managed_courselens.pyw"" -InstallRoot ""{localappdata}\CourseLens"""; WorkingDir: "{localappdata}\CourseLens\launcher"; Comment: "{#AppName}"; IconFilename: "{app}\courselens-icon.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{localappdata}\CourseLens\launcher\python312\pythonw.exe"; Parameters: "-B ""{localappdata}\CourseLens\launcher\start_managed_courselens.pyw"" -InstallRoot ""{localappdata}\CourseLens"""; WorkingDir: "{localappdata}\CourseLens\launcher"; Comment: "{#AppName}"; IconFilename: "{app}\courselens-icon.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\{#AppName}"; Filename: "{localappdata}\CourseLens\launcher\python312\pythonw.exe"; Parameters: "-B ""{localappdata}\CourseLens\launcher\start_managed_courselens.pyw"" -InstallRoot ""{localappdata}\CourseLens"""; WorkingDir: "{localappdata}\CourseLens\launcher"; Comment: "{#AppName}"; IconFilename: "{app}\courselens-icon.ico"; Tasks: quicklaunchicon

[Code]
// R-OP3: refuse to install while a CourseLens instance is serving. PascalScript
// has no raw TCP, so probe the local health endpoints through WinHttp COM.
// The /api/health contract pins service == "fudan-courselens" (start_fudan_courselens.ps1).
function HealthProbe(port: String): Boolean;
var
  http: Variant;
  resp: String;
begin
  Result := False;
  try
    http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    http.SetTimeouts(500, 500, 500, 500);
    http.Open('GET', 'http://127.0.0.1:' + port + '/api/health', False);
    http.Send;
    resp := http.ResponseText;
    Result := (http.Status = 200) and (Pos('fudan-courselens', resp) > 0);
  except
    Result := False;   // no listener on this port; keep scanning
  end;
end;

function InitializeSetup(): Boolean;
var
  port: Integer;
begin
  Result := True;
  for port := 8765 to 8775 do
    if HealthProbe(IntToStr(port)) then
    begin
      MsgBox('CourseLens 正在运行。请先退出它（关闭浏览器中的 CourseLens 页面），再重新安装。', mbConfirmation, MB_OK);
      Result := False;
      exit;
    end;
  if HealthProbe('6268') then
  begin
    MsgBox('CourseLens 正在运行。请先退出它（关闭浏览器中的 CourseLens 页面），再重新安装。', mbConfirmation, MB_OK);
    Result := False;
  end;
end;

// 卸载数据询问（默认保留学习数据=数据保护红线）。
// 交互卸载：「是」（默认/推荐）= 只卸载程序，{localappdata}\CourseLens（含 data）原地保留；
//   「否」= 还要再过一道不可恢复确认，连答两次才真删。
// 静默卸载（/SILENT、/VERYSILENT）：此询问若用普通 MsgBox 弹出，官方语义任何命令行
//   旗标都抑制不了，会挂住无人值守卸载——故静默一律不弹框，写日志并默认保留数据。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  dataRoot: String;
  deleted: Boolean;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  dataRoot := ExpandConstant('{localappdata}\CourseLens');
  if UninstallSilent() then
  begin
    Log('静默卸载：跳过数据保全询问，默认保留学习数据（' + dataRoot + ' 原地保留，不删除）。');
    exit;
  end;
  if SuppressibleMsgBox('要保留你的学习数据吗？（笔记、进度、登录状态）' #13#10 #13#10
      '「是」= 保留（推荐）：数据原地不动，随时可重装回来' #13#10
      '「否」= 一并删除：不可恢复',
      mbConfirmation, MB_YESNO, IDYES) <> IDNO then
  begin
    Log('数据保全询问：选择保留，' + dataRoot + ' 原地不动。');
    exit;
  end;
  if SuppressibleMsgBox('再次确认：真的连学习数据一起删除吗？此操作不可恢复。',
      mbConfirmation, MB_YESNO, IDYES) <> IDNO then
  begin
    Log('删除已在二次确认时取消，学习数据保留。');
    exit;
  end;
  Log('两度确认「一并删除」，开始删除 ' + dataRoot + '…');
  deleted := DelTree(dataRoot, True, True, True);
  if deleted then
    Log('学习数据已删除。')
  else
    Log('删除未完全成功：个别文件可能仍被占用，可稍后手动清理 ' + dataRoot + '。');
end;

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_managed_client.ps1"" -AllowExistingEmptyRoot -SourceRoot ""{app}"" -InstallRoot ""{localappdata}\CourseLens"""; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "正在布置课程资料目录…"

; 刻意留空：{localappdata}\CourseLens（含 data）绝不进入自动删除清单 = 数据保护红线。
; 真正的数据删除只发生在交互卸载中学生连答两次「否=一并删除」之后；静默卸载一律保留。
[UninstallDelete]
