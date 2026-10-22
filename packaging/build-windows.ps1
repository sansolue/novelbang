# Builds the Windows app (x64):
#   dist\NovelBang\                          the app folder (NovelBang.exe)
#   dist\NovelBang-<version>-x64-setup.exe   installer (needs Inno Setup 6: https://jrsoftware.org/isinfo.php)
#   dist\NovelBang-<version>-x64-portable.zip
# Run from PowerShell 7:  pwsh packaging\build-windows.ps1
# Neither the app nor the installer is code-signed, so Windows SmartScreen warns on first run.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # a failing uv/pyinstaller/iscc stops the build
Set-Location (Join-Path $PSScriptRoot "..")

uv sync --extra desktop --group build
Remove-Item -Recurse -Force build\NovelBang, dist\NovelBang -ErrorAction SilentlyContinue
uv run --extra desktop --group build pyinstaller --noconfirm --clean packaging\NovelBang.spec

$version = uv run --no-project python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
New-Item -ItemType Directory -Force build | Out-Null
uv run --group build python -c "from PIL import Image; Image.open('packaging/icon.png').save('build/icon.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 is needed for the installer: https://jrsoftware.org/isinfo.php" }
& $iscc /Q "/DAppVersion=$version" packaging\NovelBang.iss

$zip = "dist\NovelBang-$version-x64-portable.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path dist\NovelBang -DestinationPath $zip
Write-Host "Built dist\NovelBang, dist\NovelBang-$version-x64-setup.exe and $zip"
