#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
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
UninstallDisplayIcon={app}\versions\{#AppVersion}\SNSMediaCollector.exe
AppMutex=Local\SNSMediaCollectorDesktop
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "ショートカット:"; Flags: checkedonce

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SNS Media Collector"; Filename: "{app}\versions\{#AppVersion}\SNSMediaCollector.exe"; WorkingDir: "{app}\versions\{#AppVersion}"
Name: "{userdesktop}\SNS Media Collector"; Filename: "{app}\versions\{#AppVersion}\SNSMediaCollector.exe"; WorkingDir: "{app}\versions\{#AppVersion}"; Tasks: desktopicon

[Run]
Filename: "{app}\versions\{#AppVersion}\SNSMediaCollector.exe"; Description: "SNS Media Collector を起動"; Flags: nowait postinstall skipifsilent
