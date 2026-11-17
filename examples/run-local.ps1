# Self-update end to end on Windows, nothing published -- the Windows half of
# run-local.sh, for the Python demo (the Swift one is macOS only). Builds the
# demo at 1.0.0 and 1.1.0 as a onefile exe, serves 1.1.0 from a local HTTP
# server with a manifest signed by a throwaway key, starts 1.0.0 with
# auto_update on and waits for the 1.1.0 exe to report in.
#
#   examples/run-local.ps1 -Work <dir> [-Python python]
#
# `Python` must have beacon (pip install -e "python[build]") and PySide6.
# Run by .github/workflows/ci.yml (windows-e2e).
param(
    [Parameter(Mandatory)] [string] $Work,
    [string] $Python = "python"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Port = 8765
$DataDir = Join-Path $Work "data"  # given to the demo, so a test run leaves nothing in %APPDATA%
$Log = Join-Path $DataDir "update.log"
# Nuitka does not see through an editable (pip install -e) install.
$env:PYTHONPATH = Join-Path $Root "python"

# The work folder is wiped: only one this script made (or an empty one) qualifies.
$Work = [System.IO.Path]::GetFullPath($Work)
$DataDir = Join-Path $Work "data"
$Log = Join-Path $DataDir "update.log"
$Mark = Join-Path $Work ".beacon-run-local"
if ((Test-Path $Work) -and -not (Test-Path $Mark) -and (Get-ChildItem -Force $Work | Select-Object -First 1)) {
    throw "refusing to delete ${Work}: it has files and was not made by this script (no .beacon-run-local in it)"
}
Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$Work\build", "$Work\serve\py", "$Work\app" | Out-Null
New-Item -ItemType File -Force $Mark | Out-Null

$keys = & $Python -m beacon.release keygen
$env:BEACON_SIGNING_KEY = ($keys | Where-Object { $_ -like "private*" }).Split(" ")[-1]
$public = ($keys | Where-Object { $_ -like "public*" }).Split(" ")[-1]

function Build($version) {
    $out = "$Work\build\$version"
    New-Item -ItemType Directory -Force $out | Out-Null
    $config = "$Root\examples\demo-python\demo_config.json"
    @{ version = $version; public_key = $public; base_url = "http://127.0.0.1:$Port/py/"
       endpoint = "http://127.0.0.1:9/"; auto_update = $true; cask = $null; data_dir = $DataDir } |
        ConvertTo-Json | Set-Content -Encoding utf8NoBOM $config
    # The way every Python app is built: beacon.build reads examples/demo-python/beacon.toml.
    & $Python -m beacon.build --version $version --config "$Root\examples\demo-python\beacon.toml" `
        --out "$out\dist" *> "$out\build.log"
    $exe = "$out\dist\BeaconDemoPy-windows-x64.exe"
    if ($LASTEXITCODE -ne 0) { Get-Content -Tail 30 "$out\build.log"; throw "build of $version failed" }
    $exe
}

Write-Host "building..."
$old = Build "1.0.0"
$new = Build "1.1.0"
Copy-Item $new "$Work\serve\py\BeaconDemoPy.exe"
& $Python -m beacon.release manifest --repo ugurcandede/beacon-demo --tag v1.1.0 --out "$Work\serve\py" `
    --base-url "http://127.0.0.1:$Port/py/" "windows-x64=$Work\serve\py\BeaconDemoPy.exe" 2>$null

$server = Start-Process $Python -ArgumentList "-m", "http.server", $Port, "--bind", "127.0.0.1", "--directory", "$Work\serve" `
    -PassThru -WindowStyle Hidden -RedirectStandardError "$Work\server.log"
Start-Sleep 2

$exe = "$Work\app\BeaconDemoPy.exe"
Copy-Item $old $exe
Write-Host "starting 1.0.0..."
Start-Process $exe

$passed = $false
foreach ($i in 1..180) {
    if ((Test-Path $Log) -and (Select-String -Quiet -Path $Log -Pattern "check: running 1.1.0")) { $passed = $true; break }
    Start-Sleep 1
}
Start-Sleep 3  # let the new process start and clean up after the old one
$running = Get-Process BeaconDemoPy -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }

Write-Host "--- update.log"
if (Test-Path $Log) { Get-Content $Log; Copy-Item $Log "$Work\update.log" } else { Write-Host "(no update.log at $Log)" }
Write-Host "--- $Work\app"
Get-ChildItem "$Work\app" | Format-Table Name, Length, LastWriteTime

# Only the copies this run started, not a demo someone else has open.
Get-Process BeaconDemoPy -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Work*" } | Stop-Process -Force
Stop-Process $server -Force -ErrorAction SilentlyContinue

if ($passed -and $running) { Write-Host "PASS: 1.1.0 installed over 1.0.0 and running" }
else { Write-Host "FAIL: updated=$passed running=$([bool]$running)"; exit 1 }
