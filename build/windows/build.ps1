<#
.SYNOPSIS
    Builds dist\TBHprint-Setup-<version>.exe: a self-contained, per-user
    Windows installer for TBHprint (Inno Setup 6), per
    docs\DISTRIBUTION_DESIGN.md section 5.

.DESCRIPTION
    Windows PowerShell 5.1 compatible (no &&, no ?:, no ??) so it runs the
    same locally and on windows-latest GitHub-hosted runners.

    Steps (why, not just what - this is the owner's reference):
      1. Download the official python.org NuGet "python" package, pinned
         to an exact 3.12.x version with a sha256 we pin below. This is a
         full, signed CPython layout - NOT the embeddable zip, which is
         missing pieces we need (pip, ctypes) even before tkinter comes
         into it.
      2. The NuGet "python" package does NOT ship tkinter (no Lib\tkinter,
         no DLLs\_tkinter.pyd, no tcl\ runtime library) - this is a known,
         permanent property of that package, not a transient mirror issue.
         The design's fallback for this exact case is "fail loudly rather
         than ship without" tkinter (the Settings window needs it). Rather
         than dead-end there, this script pulls the missing pieces from
         the OFFICIAL python.org web-installer's "tcltk" component MSI
         (same publisher, same version, sha256-pinned the same way as the
         NuGet package) and merges them in. The loud-failure path stays as
         a real check: if tkinter is still missing after the merge (e.g.
         the tcltk download's hash no longer matches because a version
         bump forgot to update the pin below), the build stops hard.
         DEVIATION FROM THE LITERAL DESIGN TEXT - flagged for owner/
         reviewer sign-off; see the final build report.
      3. pip-install the pinned Windows wheels (requirements.txt) plus
         tbhprint itself (--no-deps; its deps are already satisfied by
         requirements.txt) into dist\win\Lib\site-packages.
      4. Fix up pywin32 (system32 DLLs next to python.exe; a .pth so
         win32/win32\lib/pythonwin resolve - pywin32 already drops its own
         pywin32.pth doing the latter, ours is a documented belt-and-braces
         copy per the design, harmless if duplicated).
      5. Generate tbhprint.ico from the app's own icon renderer.
      6. Run ISCC against tbhprint.iss to produce the setup exe.

.PARAMETER PythonVersion
    Exact NuGet "python" package version to embed. Must be a 3.12.x
    release (tkinter/Tcl-Tk sizes below are pinned to this exact version -
    bumping it means re-deriving $TcltkSha256 too).

.PARAMETER Iscc
    Path to ISCC.exe. If omitted, the script looks in the two standard
    install locations, then falls back to PATH.

.PARAMETER SignTool
.PARAMETER CertThumbprint
    Optional code-signing. Both must be given together. When absent,
    signing is skipped entirely (Setup=SIGNED is not defined, so
    tbhprint.iss's SignTool directive is compiled out - ISCC never asks
    for a signing tool it wasn't given).
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",
    [string]$Iscc = $null,
    [string]$SignTool = $null,
    [string]$CertThumbprint = $null
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Error $msg
    exit 1
}

function Assert-Sha256($path, $expected, $label) {
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $expectedLower = $expected.ToLowerInvariant()
    if ($actual -ne $expectedLower) {
        Fail "$label sha256 mismatch`n  expected: $expectedLower`n  got:      $actual`n  file:     $path`nRefusing to use an unverified download."
    }
    Write-Host "  sha256 OK ($label): $actual"
}

# -- pinned external downloads -------------------------------------------------
# Both pins were derived by downloading once from the URL shown, then
# recording the sha256 this script now checks on every run. Re-derive both
# together if $PythonVersion changes.

# https://www.nuget.org/api/v2/package/python/3.12.10 (2026-09-01)
$NuGetSha256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"

# https://www.python.org/ftp/python/3.12.10/amd64/tcltk.msi (2026-09-01)
# - the tcltk component of the official python.org web installer for the
#   SAME $PythonVersion; only used to backfill tkinter (see step 2 above).
$TcltkMsiSha256 = "55c96ffad69b1c834aa52e11b9ce41637a178ba6ad6607e83956044834276e2a"

$IcoSizes = @(16, 32, 48, 256)

# -- paths ----------------------------------------------------------------------

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BuildDir = $PSScriptRoot
$DistWin = Join-Path $RepoRoot "dist\win"
$DistOut = Join-Path $RepoRoot "dist"
$CacheDir = Join-Path $env:TEMP "tbhprint-build-cache"

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

# -- version: tbhprint/__init__.py's __version__ (pyproject.toml declares
#    version = dynamic and reads the same attribute, so this is THE source) --

$initPy = Get-Content (Join-Path $RepoRoot "tbhprint\__init__.py") -Raw
$versionMatch = [regex]::Match($initPy, '(?m)^__version__\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    Fail "could not find __version__ = `"...`" in tbhprint\__init__.py"
}
$Version = $versionMatch.Groups[1].Value
Write-Host "TBHprint version: $Version"

# -- 1. clean dist\win -----------------------------------------------------------

if (Test-Path $DistWin) {
    Write-Host "Removing existing $DistWin"
    Remove-Item -Recurse -Force $DistWin
}
New-Item -ItemType Directory -Force -Path $DistWin | Out-Null
New-Item -ItemType Directory -Force -Path $DistOut | Out-Null

# -- 2. download + verify + extract the NuGet python package ------------------

$nugetPkgPath = Join-Path $CacheDir "python.$PythonVersion.nupkg"
if (-not (Test-Path $nugetPkgPath)) {
    Write-Host "Downloading python.org NuGet package $PythonVersion ..."
    Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/python/$PythonVersion" -OutFile $nugetPkgPath -UseBasicParsing
}
Assert-Sha256 $nugetPkgPath $NuGetSha256 "python NuGet package $PythonVersion"

$nugetExtractDir = Join-Path $CacheDir "python.$PythonVersion.extracted"
if (Test-Path $nugetExtractDir) { Remove-Item -Recurse -Force $nugetExtractDir }
$nugetZipCopy = Join-Path $CacheDir "python.$PythonVersion.zip"
Copy-Item $nugetPkgPath $nugetZipCopy -Force
Expand-Archive -Path $nugetZipCopy -DestinationPath $nugetExtractDir -Force

$toolsDir = Join-Path $nugetExtractDir "tools"
if (-not (Test-Path $toolsDir)) {
    Fail "NuGet package layout has no tools\ directory - unexpected package layout for python $PythonVersion"
}
Copy-Item (Join-Path $toolsDir "*") -Destination $DistWin -Recurse -Force
Write-Host "Base CPython $PythonVersion layout copied to $DistWin"

# -- tkinter check (+ backfill from the official installer's tcltk MSI) --------

function Test-Tkinter($root) {
    (Test-Path (Join-Path $root "Lib\tkinter\__init__.py")) `
        -and (Test-Path (Join-Path $root "DLLs\_tkinter.pyd")) `
        -and (Test-Path (Join-Path $root "tcl"))
}

if (-not (Test-Tkinter $DistWin)) {
    Write-Host "tkinter missing from the NuGet layout (expected - see script header). Backfilling from the official installer's tcltk component ..."

    $tcltkMsiPath = Join-Path $CacheDir "tcltk.$PythonVersion.msi"
    if (-not (Test-Path $tcltkMsiPath)) {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/amd64/tcltk.msi" -OutFile $tcltkMsiPath -UseBasicParsing
    }
    Assert-Sha256 $tcltkMsiPath $TcltkMsiSha256 "tcltk.msi $PythonVersion"

    $tcltkExtractDir = Join-Path $CacheDir "tcltk.$PythonVersion.extracted"
    if (Test-Path $tcltkExtractDir) { Remove-Item -Recurse -Force $tcltkExtractDir }
    New-Item -ItemType Directory -Force -Path $tcltkExtractDir | Out-Null

    # "Administrative install" - unpacks the MSI's files into a directory
    # without registering/installing anything on this machine.
    $msiLog = Join-Path $CacheDir "tcltk.$PythonVersion.msiexec.log"
    $proc = Start-Process -FilePath "msiexec.exe" `
        -ArgumentList "/a", "`"$tcltkMsiPath`"", "TARGETDIR=`"$tcltkExtractDir`"", "/qn", "/log", "`"$msiLog`"" `
        -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Fail "msiexec /a tcltk.msi failed (exit $($proc.ExitCode)) - see $msiLog"
    }

    Copy-Item (Join-Path $tcltkExtractDir "DLLs\_tkinter.pyd") (Join-Path $DistWin "DLLs\") -Force
    Copy-Item (Join-Path $tcltkExtractDir "DLLs\tcl86t.dll") (Join-Path $DistWin "DLLs\") -Force
    Copy-Item (Join-Path $tcltkExtractDir "DLLs\tk86t.dll") (Join-Path $DistWin "DLLs\") -Force
    Copy-Item (Join-Path $tcltkExtractDir "DLLs\zlib1.dll") (Join-Path $DistWin "DLLs\") -Force

    New-Item -ItemType Directory -Force -Path (Join-Path $DistWin "Lib\tkinter") | Out-Null
    Copy-Item (Join-Path $tcltkExtractDir "Lib\tkinter\*") (Join-Path $DistWin "Lib\tkinter\") -Recurse -Force

    New-Item -ItemType Directory -Force -Path (Join-Path $DistWin "tcl") | Out-Null
    Copy-Item (Join-Path $tcltkExtractDir "tcl\tcl8.6") (Join-Path $DistWin "tcl\tcl8.6") -Recurse -Force
    Copy-Item (Join-Path $tcltkExtractDir "tcl\tk8.6") (Join-Path $DistWin "tcl\tk8.6") -Recurse -Force

    if (-not (Test-Tkinter $DistWin)) {
        Fail "tkinter still missing after the tcltk backfill - refusing to ship an installer without it. Check $msiLog and the tcltk.msi sha256 pin."
    }
    Write-Host "tkinter backfilled OK."
} else {
    Write-Host "tkinter present in the NuGet layout already (pin bumped past a version that lacked it?) - no backfill needed."
}

# -- 3. pip install pinned wheels + tbhprint itself -----------------------------

$sitePackages = Join-Path $DistWin "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

Write-Host "pip installing pinned Windows wheels ..."
py -3 -m pip install --target $sitePackages --only-binary=":all:" --platform win_amd64 --python-version 3.12 `
    --no-compile -r (Join-Path $BuildDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install of requirements.txt failed" }

Write-Host "pip installing tbhprint itself (--no-deps; deps came from requirements.txt) ..."
py -3 -m pip install --target $sitePackages --no-deps --no-compile $RepoRoot
if ($LASTEXITCODE -ne 0) { Fail "pip install of the tbhprint package failed" }

# -- 4. pywin32 fix-up -----------------------------------------------------------

$pywin32System32 = Join-Path $sitePackages "pywin32_system32"
if (-not (Test-Path $pywin32System32)) {
    Fail "pywin32_system32 not found under site-packages - did the pywin32 pin change layout?"
}
Copy-Item (Join-Path $pywin32System32 "*.dll") -Destination $DistWin -Force
Write-Host "Copied pywin32_system32 DLLs beside python.exe."

# pywin32's own installer normally adds win32/win32\lib/Pythonwin to
# sys.path via a post-install script that a --target install never runs;
# pywin32 does drop its own pywin32.pth doing this already, but the design
# calls for our own tbhprint.pth too (belt-and-braces if a future pywin32
# release ever stops shipping pywin32.pth under --target installs).
# No bytecode ever written into the install tree (see sitecustomize.py
# header): the uninstaller can only remove what the installer put there.
Copy-Item (Join-Path $PSScriptRoot "sitecustomize.py") (Join-Path $sitePackages "sitecustomize.py") -Force
Write-Host "Installed sitecustomize.py (dont_write_bytecode)."

$pthPath = Join-Path $sitePackages "tbhprint.pth"
Set-Content -Path $pthPath -Encoding ascii -Value @(
    "win32"
    "win32\lib"
    "pythonwin"
)
Write-Host "Wrote $pthPath"

# -- 5. tbhprint.ico --------------------------------------------------------------

$icoPath = Join-Path $DistWin "tbhprint.ico"
py -3 (Join-Path $BuildDir "gen_icon.py") $RepoRoot $icoPath
if ($LASTEXITCODE -ne 0) { Fail "icon generation failed" }

# -- 6. locate ISCC ----------------------------------------------------------------

function Find-Iscc {
    if ($Iscc) {
        if (Test-Path $Iscc) { return $Iscc }
        Fail "-Iscc `"$Iscc`" does not exist"
    }
    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $onPath = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    Fail "ISCC.exe not found - looked in the two standard Inno Setup 6 locations and PATH. Pass -Iscc <path>."
}

$isccPath = Find-Iscc
Write-Host "Using ISCC: $isccPath"

# -- 7. run ISCC --------------------------------------------------------------------

$isccArgs = @(
    (Join-Path $BuildDir "tbhprint.iss"),
    "/DMyAppVersion=$Version",
    "/DSourceDir=$DistWin",
    "/DIconFile=$icoPath",
    "/DOutputDir=$DistOut"
)

if ($SignTool -and $CertThumbprint) {
    Write-Host "Signing enabled: $SignTool (thumbprint $CertThumbprint)"
    $signCmd = "`"$SignTool`" sign /fd sha256 /sha1 $CertThumbprint /tr http://timestamp.digicert.com /td sha256 `$f"
    $isccArgs += "/DSIGNED=1"
    $isccArgs += "/Ssigntool=$signCmd"
} elseif ($SignTool -or $CertThumbprint) {
    Fail "-SignTool and -CertThumbprint must both be given (or neither) - signing skipped otherwise so partial flags are refused rather than silently ignored."
} else {
    Write-Host "No -SignTool/-CertThumbprint given - building an unsigned installer (SmartScreen will show 'unknown publisher')."
}

Write-Host "Running ISCC ..."
& $isccPath @isccArgs
if ($LASTEXITCODE -ne 0) { Fail "ISCC failed (exit $LASTEXITCODE)" }

$setupExe = Join-Path $DistOut "TBHprint-Setup-$Version.exe"
if (-not (Test-Path $setupExe)) {
    Fail "ISCC reported success but $setupExe was not produced"
}
Write-Host ""
Write-Host "Built $setupExe"
