$ErrorActionPreference = 'Stop'
$pbsRoot = $PSScriptRoot
Set-Location $pbsRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 could not create the build environment.' }
}
$pbsPython = Join-Path $pbsRoot '.venv\Scripts\python.exe'
& $pbsPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip update failed.' }
& $pbsPython -m pip install '.[build,test]'
if ($LASTEXITCODE -ne 0) { throw 'Build dependencies could not be installed.' }
$env:QT_QPA_PLATFORM = 'offscreen'
$env:QT_MEDIA_BACKEND = 'ffmpeg'
try {
    & $pbsPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; the package was not built.' }
    & $pbsPython -m PyInstaller --clean --noconfirm PlaguesBroadcastStation.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
} finally {
    Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
}
Copy-Item 'run-windows.bat' 'dist\PlaguesBroadcastStation\run-windows.bat' -Force
Copy-Item 'README.md','THIRD_PARTY_NOTICES.md','requirements-lyrics.txt' 'dist\PlaguesBroadcastStation' -Force
Copy-Item 'docs' 'dist\PlaguesBroadcastStation' -Recurse -Force
New-Item -ItemType Directory -Path 'artifacts' -Force | Out-Null
Compress-Archive -Path 'dist\PlaguesBroadcastStation' -DestinationPath 'artifacts\PlaguesBroadcastStation-windows-x86_64.zip' -Force
Write-Host 'Built artifacts\PlaguesBroadcastStation-windows-x86_64.zip'
