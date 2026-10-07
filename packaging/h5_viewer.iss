#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir must point to the directory containing H5Viewer.exe
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

#define MyAppName "H5 Viewer"
#define MyAppExeName "H5Viewer.exe"
#define MyAppPublisher "H5 Viewer contributors"

[Setup]
AppId={{A6F91DAA-A59A-4D6F-B43B-55231790384A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppVerName={#MyAppName} {#MyAppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
Compression=lzma2/ultra64
DefaultDirName={localappdata}\Programs\H5Viewer
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=H5Viewer-{#MyAppVersion}-windows-x64-setup
OutputDir={#OutputDir}
PrivilegesRequired=lowest
SetupLogging=yes
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UsePreviousAppDir=yes
WizardStyle=modern

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove only application-owned paths so stale onedir files cannot survive an upgrade.
Type: files; Name: "{app}\{#MyAppExeName}"
Type: files; Name: "{app}\README.md"
Type: files; Name: "{app}\USER_GUIDE.md"
Type: files; Name: "{app}\THIRD_PARTY_NOTICES.txt"
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Register with Default Apps and Open With without writing an extension default.
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "H5 Viewer"; ValueData: "Software\H5Viewer\Capabilities"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\H5Viewer\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "H5 Viewer"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\H5Viewer\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Read-only HDF5 file explorer"
Root: HKCU; Subkey: "Software\H5Viewer\Capabilities\FileAssociations"; ValueType: string; ValueName: ".h5"; ValueData: "H5Viewer.h5"
Root: HKCU; Subkey: "Software\H5Viewer\Capabilities\FileAssociations"; ValueType: string; ValueName: ".hdf5"; ValueData: "H5Viewer.hdf5"

Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "H5 Viewer"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: none; ValueName: ".h5"
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: none; ValueName: ".hdf5"
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

Root: HKCU; Subkey: "Software\Classes\H5Viewer.h5"; ValueType: string; ValueName: ""; ValueData: "HDF5 file"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\H5Viewer.h5\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"",0"
Root: HKCU; Subkey: "Software\Classes\H5Viewer.h5\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.h5\OpenWithProgids"; ValueType: none; ValueName: "H5Viewer.h5"; Flags: uninsdeletevalue

Root: HKCU; Subkey: "Software\Classes\H5Viewer.hdf5"; ValueType: string; ValueName: ""; ValueData: "HDF5 file"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\H5Viewer.hdf5\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"",0"
Root: HKCU; Subkey: "Software\Classes\H5Viewer.hdf5\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.hdf5\OpenWithProgids"; ValueType: none; ValueName: "H5Viewer.hdf5"; Flags: uninsdeletevalue

[Run]
Filename: "ms-settings:defaultapps"; Description: "Choose default apps in Windows Settings"; Flags: postinstall skipifsilent unchecked nowait shellexec

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('H5 Viewer requires 64-bit Windows.', mbError, MB_OK);
    Result := False;
  end;
end;
