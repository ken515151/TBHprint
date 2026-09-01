; TBHprint Windows installer (Inno Setup 6).
; docs\DISTRIBUTION_DESIGN.md section 5 is binding for the shape of this
; file - read that before changing anything here.
;
; Built by build.ps1, which passes:
;   /DMyAppVersion=<from pyproject.toml>
;   /DSourceDir=<dist\win, the fully-assembled Python + tbhprint tree>
;   /DIconFile=<dist\win\tbhprint.ico>
;   /DOutputDir=<dist>
;   /DSIGNED=1 and /Ssigntool=<cmd> only when -SignTool/-CertThumbprint
;     were both given (otherwise the SignTool directives below are
;     compiled out - ISCC never demands a signing tool it wasn't handed).
;
; Not meant to be compiled by hand without those defines.

#ifndef MyAppVersion
  #error "Pass /DMyAppVersion=x.y.z (build.ps1 does this for you)"
#endif
#ifndef SourceDir
  #error "Pass /DSourceDir=<path to the assembled dist\win tree>"
#endif
#ifndef IconFile
  #error "Pass /DIconFile=<path to tbhprint.ico>"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
; Fixed forever - Inno/Windows use this to recognise "the same app" across
; versions (upgrades, uninstall-entry identity). Generated once with
; [guid]::NewGuid(); never regenerate it.
AppId={{DF38A9D0-54BF-4D40-88CC-11AFB7505FD7}
AppName=TBHprint
AppVersion={#MyAppVersion}
AppVerName=TBHprint {#MyAppVersion}
AppPublisher=TechBenchHub
AppPublisherURL=https://techbenchhub.co.uk
VersionInfoVersion={#MyAppVersion}

; Per-user, no admin prompt ever - this is the owner's #1 install-story
; ruling ("no command line, ever").
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\TBHprint
DefaultGroupName=TBHprint
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousAppDir=yes

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; The bundled python.exe/pythonw.exe/tray may already be running (this is
; itself an upgrade path - see docs section 4 auto-update) - close them
; (and anything else with an open handle on our files) rather than fail,
; and bring the tray back afterwards.
CloseApplications=yes
RestartApplications=yes

SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\tbhprint.ico
UninstallDisplayName=TBHprint

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir={#OutputDir}
OutputBaseFilename=TBHprint-Setup-{#MyAppVersion}

; Unsigned by default (no code-signing certificate yet - see build.ps1
; header and docs section 5). Only present when build.ps1 was given
; -SignTool/-CertThumbprint; ISCC would otherwise refuse to compile a
; script that names a signtool it was never handed.
#ifdef SIGNED
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; The whole assembled runtime: python.exe/pythonw.exe, DLLs, Lib
; (including site-packages and tkinter), tcl\, tbhprint.ico.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; The ONE Start Menu entry. Never just the tray icon - first run (and
; every subsequent launch from here) opens Settings, per the owner's
; ruling that the tray is never the only door in.
; "-B": never write __pycache__ into the install tree - this is a
; per-user directory Python happily writes .pyc caches into at import
; time, which would otherwise survive Inno's uninstall (it only removes
; files it installed, not ones the program wrote later) and leave an
; empty-looking but non-empty {app} behind.
Name: "{group}\TBHprint"; Filename: "{app}\pythonw.exe"; Parameters: "-B -m tbhprint settings"; WorkingDir: "{app}"; IconFilename: "{app}\tbhprint.ico"
Name: "{group}\Uninstall TBHprint"; Filename: "{uninstallexe}"

[Registry]
; Start the tray (supervisor) at logon. It opens Settings itself, on its
; own, the first time it finds the daemon unpaired - no separate
; "first run" Start Menu click is required for that to happen.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TBHprint"; ValueData: """{app}\pythonw.exe"" -B -m tbhprint tray"; Flags: uninsdeletevalue

[Run]
; Runs after both a fresh install AND a silent /VERYSILENT re-run (i.e.
; the auto-update path in docs section 4) - deliberately NOT flagged
; skipifsilent, because a silent auto-update install must bring the tray
; back on its own with nobody watching the wizard.
Filename: "{app}\pythonw.exe"; Parameters: "-B -m tbhprint tray"; Flags: nowait postinstall

[UninstallRun]
; Stop the tray + supervised agent cleanly before files are removed.
Filename: "{app}\python.exe"; Parameters: "-B -m tbhprint quit"; RunOnceId: "StopTBHprint"; Flags: runhidden skipifdoesntexist

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StateDir: String;
  AppDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Sweep FIRST, unconditionally: Inno only removes the files/dirs it
    // installed, not anything Python wrote at runtime (__pycache__ from a
    // hand-run python.exe - our own launches use -B). By this point every
    // tracked file is already gone, so whatever is left under {app} is
    // safe to take with it rather than leave a phantom install directory.
    AppDir := ExpandConstant('{app}');
    if DirExists(AppDir) then
      DelTree(AppDir, True, True, True);
    // Config, pairing token, print history and logs - separate from the
    // program files just removed. Ask, don't assume: a shop reinstalling
    // TBHprint to point it at the same printers again will want this kept.
    // A silent uninstall (auto-update path, scripted removal) never asks
    // and always keeps them - there is nobody there to answer.
    StateDir := ExpandConstant('{localappdata}\TBHprint');
    if DirExists(StateDir) and (not UninstallSilent) then
    begin
      if MsgBox('Also remove TBHprint''s settings, pairing and logs (' + StateDir + ')?' + #13#10 + 'Choose No to keep them for a future reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(StateDir, True, True, True);
    end;
  end;
end;
