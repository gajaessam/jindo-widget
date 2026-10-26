#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef AppBundle
  #define AppBundle "dist\JindoWidget"
#endif

[Setup]
AppId={{B96128C4-53BB-48FE-A357-A87F7B09D641}
AppName=진도 위젯
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\JindoWidget
DefaultGroupName=진도 위젯
UninstallDisplayName=진도 위젯
UninstallDisplayIcon={app}\JindoWidget.exe
OutputDir=release
OutputBaseFilename=진도 위젯-v{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로 가기 만들기"; GroupDescription: "추가 아이콘:"; Flags: unchecked

[Files]
Source: "{#AppBundle}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\UsagiJindoUpdater.exe"
Type: filesandordirs; Name: "{app}\_internal\tkinterdnd2"

[Icons]
Name: "{autoprograms}\진도 위젯"; Filename: "{app}\JindoWidget.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\진도 위젯"; Filename: "{app}\JindoWidget.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\JindoWidget.exe"; Description: "진도 위젯 실행"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'JindoWidget');
end;
