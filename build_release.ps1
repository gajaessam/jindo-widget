param([string]$PythonExe = "python")

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $PSScriptRoot

& $PythonExe -c "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy(); import PIL"
if ($LASTEXITCODE -ne 0) {
    throw "선택한 Python에 정상적인 Tcl/Tk와 requirements-build.txt 패키지가 필요합니다."
}
& $PythonExe -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    throw "선택한 Python에 PyInstaller가 없습니다. requirements-build.txt를 설치해 주세요."
}

$Version = (& $PythonExe -c "from update_config import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "update_config.py의 APP_VERSION을 확인해 주세요."
}
$AppName = (& $PythonExe -c "from update_config import APP_FOLDER_NAME; print(APP_FOLDER_NAME)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $AppName) {
    throw "update_config.py의 APP_FOLDER_NAME을 확인해 주세요."
}

$DistRoot = Join-Path $PSScriptRoot "dist"
$BuildRoot = Join-Path $PSScriptRoot "build"
$ReleaseRoot = Join-Path $PSScriptRoot "release"
$AppBundle = Join-Path $DistRoot $AppName
$InstallerPath = Join-Path $ReleaseRoot "진도 위젯-v$Version.exe"

New-Item -ItemType Directory -Force -Path `
    $BuildRoot, $ReleaseRoot, (Join-Path $BuildRoot "app-spec") | Out-Null
if (Test-Path -LiteralPath $AppBundle) {
    Remove-Item -LiteralPath $AppBundle -Recurse -Force
}
Get-ChildItem -LiteralPath $ReleaseRoot -Filter "UsagiJindo-v*-Windows-x64.zip" `
    -ErrorAction SilentlyContinue | Remove-Item -Force

& $PythonExe -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name $AppName --distpath $DistRoot `
    --workpath (Join-Path $BuildRoot "app") `
    --specpath (Join-Path $BuildRoot "app-spec") app.py
if ($LASTEXITCODE -ne 0) { throw "앱 빌드에 실패했어요." }

if (-not (Test-Path -LiteralPath (Join-Path $AppBundle "$AppName.exe"))) {
    throw "빌드된 앱 실행 파일을 찾지 못했어요."
}

$IsccCandidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe")
)
$IsccPath = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $IsccPath) {
    $IsccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($IsccCommand) { $IsccPath = $IsccCommand.Source }
}
if (-not $IsccPath) {
    throw "Inno Setup Compiler(ISCC.exe)를 찾지 못했어요. GitHub Actions의 windows-latest에서 다시 실행해 주세요."
}

& $IsccPath "/DAppVersion=$Version" "/DAppBundle=$AppBundle" "installer.iss"
if ($LASTEXITCODE -ne 0) { throw "설치 파일 빌드에 실패했어요." }
if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "빌드된 설치 파일을 찾지 못했어요."
}
$InstallerHash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "설치 파일 준비 완료: $InstallerPath"
Write-Output "SHA-256: $InstallerHash"
