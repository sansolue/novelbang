; Inno Setup script for the NovelBang Windows installer. Built by packaging/build-windows.ps1,
; which passes the version (ISCC /DAppVersion=0.2.0 packaging\NovelBang.iss) and, to sign, /DSign.
;
; Installs for the current user (no admin rights needed) into %LOCALAPPDATA%\Programs\NovelBang,
; with a Start menu entry and an uninstaller. Uninstalling leaves the novels alone.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise upgrades of the same app.
AppId={{6B850059-D35D-45CC-8961-5EF39C87317E}
AppName=NovelBang
AppVersion={#AppVersion}
AppVerName=NovelBang {#AppVersion}
AppPublisher=SanSolue
AppPublisherURL=https://github.com/sansolue/novelbang
DefaultDirName={autopf}\NovelBang
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=NovelBang-{#AppVersion}-x64-setup
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\NovelBang.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
#ifdef Sign
; build-windows.ps1 defines the "signtool" command (ISCC /Ssigntool=…) when a certificate is set.
SignTool=signtool
SignedUninstaller=yes
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\NovelBang\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files left by an older version that this one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\NovelBang"; Filename: "{app}\NovelBang.exe"
Name: "{autodesktop}\NovelBang"; Filename: "{app}\NovelBang.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NovelBang.exe"; Description: "{cm:LaunchProgram,NovelBang}"; Flags: nowait postinstall skipifsilent
