#define MyAppName "Any Link Downloader"
; build.py passes the real version in from ald/core.py, this one is just a fallback
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppExe "AnyLinkDownloader.exe"
#define MyAppPublisher "X-4bys5"
#define MyAppURL "https://github.com/X-4bys5/AnyLinkDownloader"

[Setup]
; dont change this id, windows uses it to see that a new setup is an upgrade of the same app
AppId={{6D1F2B4A-3C7E-4A9B-8E15-5B7C2A9D4F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} setup
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; no admin needed by default, the dialog lets them pick all users if they want
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=AnyLinkDownloader-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; shuts the running app down so its files can be replaced
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\AnyLinkDownloader\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[InstallDelete]
; old bundle and the downloaded yt-dlp engine get cleared so an update starts clean
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{localappdata}\AnyLinkDownloader\engine"
Type: filesandordirs; Name: "{localappdata}\AnyLinkDownloader\engine.new"
; a new installer brings its own ald/, an old quick update must not sit on top of it
Type: filesandordirs; Name: "{localappdata}\AnyLinkDownloader\patch"

[UninstallDelete]
; this removes settings, queue and logs too
Type: filesandordirs; Name: "{localappdata}\AnyLinkDownloader"

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start {#MyAppName}"; Flags: nowait postinstall
