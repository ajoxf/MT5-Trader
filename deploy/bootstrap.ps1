<#
    Prepares a fresh Windows EC2 instance to run MT5-Trader.

    Run once, in an elevated PowerShell:

        Set-ExecutionPolicy -Scope Process Bypass -Force
        .\deploy\bootstrap.ps1

    What it does NOT do, on purpose: install MetaTrader 5 or log it in.
    Each account needs its own terminal installation and its own login,
    and choosing which account goes where is the one decision that must
    not be automated — two accounts pointing at one terminal folder are
    one account whatever the config says.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host "MT5-Trader — preparing this machine" -ForegroundColor Cyan

# --- Python 3.11, 64-bit. It must match the 64-bit terminal or the
#     MT5 IPC handshake fails with an error that says nothing useful.
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Python 3.11 (64-bit)..."
    $installer = Join-Path $env:TEMP 'python-3.11.exe'
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' `
        -OutFile $installer
    Start-Process -Wait -FilePath $installer -ArgumentList `
        '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0'
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
} else {
    Write-Host "Python is already installed."
}

Write-Host "Installing dependencies..."
Push-Location $root
py -3.11 -m pip install --upgrade pip
py -3.11 -m pip install -r requirements.txt

if (-not (Test-Path (Join-Path $root 'config.json'))) {
    Copy-Item (Join-Path $root 'config.example.json') (Join-Path $root 'config.json')
    Write-Host "Created config.json from the example."
}
if (-not (Test-Path (Join-Path $root '.env'))) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
}

Write-Host "Running the safety tests..."
py -3.11 -m pytest tests -q
if ($LASTEXITCODE -ne 0) {
    throw "The test suite failed. Do not trade on this build."
}
Pop-Location

# --- A desktop shortcut, because the operator should never have to
#     find a folder or type a command.
$shortcut = Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) `
    'START TRADING.lnk'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = Join-Path $root 'START-TRADING.bat'
$link.WorkingDirectory = $root
$link.Description = 'Start MT5-Trader and open the ladders'
$link.Save()

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Still to do by hand, once:"
Write-Host "  1. Install MetaTrader 5 TWICE, into separate folders"
Write-Host "     (C:\MT5-A and C:\MT5-B), and log each into its account."
Write-Host "  2. Press Algo Trading in each terminal so it turns green."
Write-Host "  3. Do NOT run the terminals as Administrator — a terminal"
Write-Host "     started elevated will not accept a connection from a"
Write-Host "     normally-started Python."
Write-Host "  4. Double-click START TRADING on the desktop, then fill in"
Write-Host "     the two accounts on the Exchanges page."
