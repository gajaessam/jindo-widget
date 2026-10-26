param([string]$PythonExe = "python")

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $PSScriptRoot

& $PythonExe -c "import tkinter; root=tkinter.Tk(); root.withdraw(); root.destroy(); import PIL"
if ($LASTEXITCODE -ne 0) {
    throw "선택한 Python에 정상적인 Tcl/Tk와 Pillow가 필요합니다. Python.org의 64비트 설치본에서 Tcl/Tk 기능을 포함해 설치한 뒤 -PythonExe로 경로를 지정해 주세요."
}
& $PythonExe -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    throw "선택한 Python에 PyInstaller가 없습니다. requirements-build.txt를 설치해 주세요."
}

$Version = (& $PythonExe -c "from update_config import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "update_config.py의 APP_VERSION을 확인해 주세요."
}
$Repository = (& $PythonExe -c "from update_config import GITHUB_REPOSITORY; print(GITHUB_REPOSITORY)").Trim()
if ($LASTEXITCODE -ne 0 -or $Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "update_config.py의 GITHUB_REPOSITORY가 설정되지 않았어요."
}

$DistRoot = Join-Path $PSScriptRoot "dist"
$BuildRoot = Join-Path $PSScriptRoot "build"
$ReleaseRoot = Join-Path $PSScriptRoot "release"
$PackageRoot = Join-Path $BuildRoot "release-package"
$AppName = (& $PythonExe -c "from update_config import APP_FOLDER_NAME; print(APP_FOLDER_NAME)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $AppName) {
    throw "update_config.py의 APP_FOLDER_NAME을 확인해 주세요."
}
$AppBundle = Join-Path $DistRoot $AppName
$UpdaterOutput = Join-Path $DistRoot "UsagiJindoUpdater.exe"
$UpdaterInBundle = Join-Path $AppBundle "UsagiJindoUpdater.exe"
$ArchivePath = Join-Path $ReleaseRoot "UsagiJindo-v$Version-Windows-x64.zip"

New-Item -ItemType Directory -Force -Path `
    $BuildRoot, $ReleaseRoot, (Join-Path $BuildRoot "app-spec"), `
    (Join-Path $BuildRoot "updater-spec") | Out-Null
if (Test-Path -LiteralPath $AppBundle) {
    Remove-Item -LiteralPath $AppBundle -Recurse -Force
}
if (Test-Path -LiteralPath $UpdaterOutput) {
    Remove-Item -LiteralPath $UpdaterOutput -Force
}
if (Test-Path -LiteralPath $PackageRoot) {
    Remove-Item -LiteralPath $PackageRoot -Recurse -Force
}

& $PythonExe -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name $AppName --distpath $DistRoot `
    --workpath (Join-Path $BuildRoot "app") `
    --specpath (Join-Path $BuildRoot "app-spec") app.py
if ($LASTEXITCODE -ne 0) { throw "앱 빌드에 실패했어요." }

& $PythonExe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "UsagiJindoUpdater" --distpath $DistRoot `
    --workpath (Join-Path $BuildRoot "updater") `
    --specpath (Join-Path $BuildRoot "updater-spec") updater.py
if ($LASTEXITCODE -ne 0) { throw "업데이트 도우미 빌드에 실패했어요." }

if (-not (Test-Path -LiteralPath (Join-Path $AppBundle "$AppName.exe"))) {
    throw "빌드된 앱 실행 파일을 찾지 못했어요."
}
if (-not (Test-Path -LiteralPath $UpdaterOutput)) {
    throw "빌드된 업데이트 도우미를 찾지 못했어요."
}
Copy-Item -LiteralPath $UpdaterOutput -Destination $UpdaterInBundle -Force

if (Test-Path -LiteralPath $ArchivePath) {
    Remove-Item -LiteralPath $ArchivePath -Force
}
New-Item -ItemType Directory -Path $PackageRoot | Out-Null
Copy-Item -LiteralPath $AppBundle -Destination $PackageRoot -Recurse
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $PackageRoot, $ArchivePath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
$Hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "릴리스 준비 완료: $ArchivePath"
Write-Output "SHA-256: $Hash"
