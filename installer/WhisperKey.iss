#define MyAppName "WhisperKey"
#ifndef MyAppVersion
#define MyAppVersion "0.9.0"
#endif
#define MyAppPublisher "WhisperKey"
#define MyAppExeName "WhisperKey.exe"

[Setup]
AppId={{2470E795-33BC-45B5-9D78-9C05014FB825}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\WhisperKey
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir=..\artifacts\release
OutputBaseFilename=WhisperKey-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\src\whisper_key\platform\windows\assets\whisperkey-icon.ico
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
UsePreviousLanguage=yes
UsePreviousTasks=yes
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} local-first voice and knowledge capture

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\WhisperKey\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE-WhisperKey.txt"; Flags: ignoreversion
Source: "..\src\whisper_key\assets\PRIVACY_RECORDING_NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\artifacts\release\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\artifacts\release\THIRD_PARTY_LICENSES\*"; DestDir: "{app}\THIRD_PARTY_LICENSES"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\artifacts\release\OFFLINE_STARTUP_EVIDENCE.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\artifacts\release\release-manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\artifacts\release\sbom.cdx.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\artifacts\release\SHA256SUMS.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName}"; Flags: nowait postinstall skipifsilent
