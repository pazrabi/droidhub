param([Parameter(Mandatory)][string] $AppDir)

$ErrorActionPreference = 'Stop'
$tools = Join-Path $AppDir '_internal/tools'
$adb = Join-Path $tools 'adb.exe'
$scrcpy = Join-Path $tools 'scrcpy.exe'
foreach ($asset in @(
    'DroidHub.exe',
    '_internal/certifi/cacert.pem',
    '_internal/tools/scrcpy-server',
    '_internal/tools/AdbWinApi.dll',
    '_internal/tools/AdbWinUsbApi.dll',
    '_internal/tools/SDL3.dll',
    '_internal/tools/LICENSE.txt'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir $asset) -PathType Leaf)) {
        throw "Missing packaged file $asset"
    }
}

& $adb version
if ($LASTEXITCODE -ne 0) { throw "Bundled ADB exited with $LASTEXITCODE" }
& $scrcpy --version
if ($LASTEXITCODE -ne 0) { throw "Bundled scrcpy exited with $LASTEXITCODE" }

$process = Start-Process -FilePath (Join-Path $AppDir 'DroidHub.exe') -WorkingDirectory $env:RUNNER_TEMP -PassThru
try {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
    } until ($process.HasExited -or $process.MainWindowHandle -ne 0 -or $timer.Elapsed.TotalSeconds -ge 25)
    if ($process.HasExited) { throw "DroidHub exited early with $($process.ExitCode)" }
    if ($process.MainWindowTitle -notlike 'DroidHub*USB*') { throw "DroidHub main window did not appear" }
    if (-not $process.CloseMainWindow() -or -not $process.WaitForExit(15000)) {
        throw "DroidHub did not close cleanly"
    }
    if ($process.ExitCode -ne 0) { throw "DroidHub closed with $($process.ExitCode)" }
    Write-Host 'DroidHub GUI opened and closed successfully.'
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    & $adb kill-server
}
