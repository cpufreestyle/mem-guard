; MemGuard 安装脚本（Inno Setup 6）
;
; 编译（CI 里执行，版本号从 memguard/config.py 读取后注入，避免发版时忘改）：
;   ISCC.exe /DAppVersion=1.3.12 installer\mem_guard.iss
;
; 说明：
;   - 采用「当前用户」安装（PrivilegesRequired=lowest），安装无需管理员/UAC，
;     配置与日志也能正常写入安装目录（MemGuard 清理时才需要手动提权运行）。
;   - iss 内的相对路径均以本文件所在目录（installer/）为基准。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "MemGuard"
#define AppPublisher "cpufreestyle"
#define AppURL "https://github.com/cpufreestyle/mem-guard"

[Setup]
AppId={{9C4B2B1E-6F3A-4C1D-9F2E-A1B2C3D4E5F6}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=MemGuard-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\mem_guard.exe
SetupIconFile=..\mem_guard.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\mem_guard.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\mem_guard.exe"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\mem_guard.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\mem_guard.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
