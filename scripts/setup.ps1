param(
    [string]$Ref = 'main',
    [string]$SourceDirectory = '',
    [string]$PythonExecutable = '',
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Programs\SidePulse')
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use scripts/setup.sh on macOS or Linux.' }
$python = Join-Path $InstallDirectory 'venv\Scripts\python.exe'
$cli = Join-Path $InstallDirectory 'venv\Scripts\sidepulse.exe'
$wasRunning = $false
$oldVersion = $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE
$oldMetadata = $env:SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE
try {
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        $candidate = $null
        if ($PythonExecutable) {
            if (!(Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'The selected Python executable does not exist.' }
            & $PythonExecutable -c "import sys; assert sys.version_info >= (3,10)"
            if ($LASTEXITCODE -ne 0) { throw 'The selected Python must be version 3.10 or newer.' }
            $candidate = $PythonExecutable
        }
        foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
            if ($candidate) { break }
            $command = Get-Command $name -ErrorAction SilentlyContinue
            if (!$command -or $command.Source -like '*\WindowsApps\*') { continue }
            $args = if ($name -eq 'py.exe') { @('-3') } else { @() }
            $value = & $command.Source @args -c "import sys; assert sys.version_info >= (3,10); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $value) { $candidate = [string]$value; break }
        }
        if (!$candidate) {
            throw 'SidePulse needs Python 3.10 or newer. Install Python from https://www.python.org/downloads/windows/ (enable pip), open a new terminal, and run this installer again. You can also use: winget install --id Python.Python.3.13 --scope user'
        }
        New-Item -ItemType Directory -Force -Path $InstallDirectory | Out-Null
        & $candidate -m venv (Join-Path $InstallDirectory 'venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the private Python environment; verify that Python includes venv and pip.' }
    }
    & $python -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10 or newer is required'"
    if ($LASTEXITCODE -ne 0) { throw 'The existing SidePulse interpreter is unsupported; it has been retained.' }
    if ($SourceDirectory) {
        $package = (Resolve-Path -LiteralPath $SourceDirectory).Path
        if (!(Test-Path -LiteralPath (Join-Path $package 'pyproject.toml'))) { throw 'SourceDirectory must contain the SidePulse project.' }
        $context = Join-Path $package 'build-context.json'
        if (Test-Path -LiteralPath $context) {
            $revision = (Get-Content -Raw -LiteralPath $context | ConvertFrom-Json).revision
            if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Invalid frozen source revision.' }
            $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE = '0+windowstest'
            $env:SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE = "{node=`"g$revision`"}"
        }
    } else {
        $refPart = [Uri]::EscapeDataString($Ref)
        $revision = (Invoke-RestMethod -Uri "https://api.github.com/repos/inteliwear/sidepulse/commits/$refPart").sha
        if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'The requested SidePulse source could not be resolved.' }
        $package = "https://github.com/inteliwear/sidepulse/archive/$revision.zip"
        $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE = '0+g' + $revision.Substring(0,12)
        $env:SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE = "{node=`"g$revision`"}"
    }
    if (Test-Path -LiteralPath $cli) {
        & $cli service status *> $null
        $wasRunning = $LASTEXITCODE -eq 0
        if ($wasRunning) {
            & $cli service stop
            if ($LASTEXITCODE -ne 0) { throw 'SidePulse could not stop; the existing installation has been retained.' }
            & $cli service status *> $null
            if ($LASTEXITCODE -eq 0) { throw 'SidePulse is still running; the existing installation has been retained.' }
        }
    }
    & $python -m pip install --upgrade --disable-pip-version-check $package
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Existing user settings have been retained; inspect the package installer output above.' }
    & $cli setup
    if ($LASTEXITCODE -ne 0) { throw 'SidePulse setup failed; inspect the instructions above.' }
    & $cli service status
    if ($LASTEXITCODE -ne 0) { throw 'The background service did not start. Inspect the service logs shown by sidepulse setup.' }
    $bin = Split-Path $cli
    $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
    if (@($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') }).Count -eq 0) {
        [Environment]::SetEnvironmentVariable('PATH', "$bin;$userPath", 'User')
    }
    Write-Host 'SidePulse is installed. Open a new terminal and run: sidepulse status'
} catch {
    if ($wasRunning -and (Test-Path -LiteralPath $cli)) { & $cli service start }
    throw
} finally {
    $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE = $oldVersion
    $env:SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE = $oldMetadata
}
