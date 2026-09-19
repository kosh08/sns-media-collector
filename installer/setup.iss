#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef LauncherExe
  #error LauncherExe is required
#endif

[Setup]
AppId=SNSMediaCollector.MasterTools.Desktop
AppName=SNS Media Collector
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\SNSMediaCollector
DefaultGroupName=SNS Media Collector
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir={#OutputDir}
OutputBaseFilename=SNSMediaCollector-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SNSMediaCollector.exe
AppMutex=Local\SNSMediaCollectorDesktop
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#LauncherExe}"; DestDir: "{app}"; DestName: "SNSMediaCollector.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\SNS Media Collector"; Filename: "{app}\SNSMediaCollector.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\SNS Media Collector"; Filename: "{app}\SNSMediaCollector.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\versions\{#AppVersion}\bin\SNSMediaCollectorUpdater.exe"; Parameters: "--cleanup-versions ""{app}\versions"" ""{#AppVersion}"""; Flags: runhidden waituntilterminated
Filename: "{app}\SNSMediaCollector.exe"; Description: "SNS Media Collector を起動"; Flags: nowait postinstall skipifsilent
