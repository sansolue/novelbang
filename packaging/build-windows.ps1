# Builds the Windows app (x64):
#   dist\NovelBang\                          the app folder (NovelBang.exe)
#   dist\NovelBang-<version>-x64-setup.exe   installer (needs Inno Setup 6: https://jrsoftware.org/isinfo.php)
#   dist\NovelBang-<version>-x64-portable.zip
# Run from PowerShell 7:  pwsh packaging\build-windows.ps1
#
# Unsigned by default, so Windows SmartScreen warns on first run. To sign NovelBang.exe, the
# installer and the uninstaller, make the code-signing certificate available in the Windows
# certificate store (your signing provider's tool does this) and set (see README):
#   WINDOWS_SIGN_CERT            the certificate's SHA-1 thumbprint
#   WINDOWS_SIGN_TIMESTAMP_URL   optional, your provider's RFC 3161 timestamp server
#   WINDOWS_SIGNTOOL             optional, path to signtool.exe (found in the Windows SDK otherwise)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # a failing uv/pyinstaller/iscc/signtool stops the build
Set-Location (Join-Path $PSScriptRoot "..")

$cert = ($env:WINDOWS_SIGN_CERT ?? "") -replace "\s", ""
if ($cert) {
    $signtool = $env:WINDOWS_SIGNTOOL
    if (-not $signtool) {
        $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
            Sort-Object { [version]$_.Directory.Parent.Name } | Select-Object -Last 1 -ExpandProperty FullName
    }
    if (-not $signtool -or -not (Test-Path $signtool)) { throw "signtool.exe not found: install the Windows SDK or set WINDOWS_SIGNTOOL" }
    $timestamp = $env:WINDOWS_SIGN_TIMESTAMP_URL ?? "http://timestamp.digicert.com"
    $signArgs = @("sign", "/sha1", $cert, "/fd", "sha256", "/tr", $timestamp, "/td", "sha256", "/d", "NovelBang")
    function Sign-File($path) {
        & $signtool @signArgs $path
        & $signtool verify /pa /q $path
    }
}

uv sync --extra desktop --group build
Remove-Item -Recurse -Force build\NovelBang, dist\NovelBang -ErrorAction SilentlyContinue
uv run --extra desktop --group build pyinstaller --noconfirm --clean packaging\NovelBang.spec
if ($cert) { Sign-File dist\NovelBang\NovelBang.exe }

$version = uv run --no-project python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
New-Item -ItemType Directory -Force build | Out-Null
uv run --group build python -c "from PIL import Image; Image.open('packaging/icon.png').save('build/icon.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 is needed for the installer: https://jrsoftware.org/isinfo.php" }
$isccArgs = @("/Q", "/DAppVersion=$version")
if ($cert) {
    # Inno Setup signs the installer and the uninstaller it writes, with this command ($f is the file).
    $quoted = ($signArgs | ForEach-Object { if ($_ -match "\s") { "`$q$_`$q" } else { $_ } }) -join " "
    $isccArgs += "/DSign", "/Ssigntool=`$q$signtool`$q $quoted `$f"
}
& $iscc @isccArgs packaging\NovelBang.iss
if ($cert) { & $signtool verify /pa /q "dist\NovelBang-$version-x64-setup.exe" }

$zip = "dist\NovelBang-$version-x64-portable.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path dist\NovelBang -DestinationPath $zip
$signed = if ($cert) { "signed" } else { "unsigned" }
Write-Host "Built dist\NovelBang, dist\NovelBang-$version-x64-setup.exe and $zip ($signed)"
