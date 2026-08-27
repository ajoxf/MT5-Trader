<#
    Prepares a Windows EC2 instance to run MT5-Trader.

    Run once, in PowerShell, from the folder you cloned into:

        Set-ExecutionPolicy -Scope Process Bypass -Force
        .\deploy\bootstrap.ps1

    This file is deliberately plain ASCII and single-quoted throughout.
    Windows PowerShell 5.1 reads a script that has no byte-order mark as
    ANSI, so one stray dash or quote from a word processor comes back as
    a parser error on a line that looks perfectly fine - which is a
    miserable way to lose an afternoon on a box that is otherwise ready.

    What it does NOT do, on purpose: install MetaTrader 5 or log it in.
    Each account needs its own terminal installation and its own login,
    and choosing which account goes where is the one decision that must
    not be automated - two accounts pointing at one terminal folder are
    one account whatever the config says.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host 'MT5-Trader: preparing this machine' -ForegroundColor Cyan

function Find-Python {
    <#
        The command that runs Python here, as a list: the program and
        any arguments it needs.

        Three ways a working Python turns up on these boxes, and all
        three are normal:
          * the launcher, 'py -3.11', on a plain python.org install;
          * 'python' on PATH, which is what a conda or venv prompt has;
          * neither, on a fresh instance, which is what the install
            below is for.
        Assuming the launcher is why a machine that already had Python
        was told it had none.
    #>
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -c 'import sys' 2>$null
        if ($LASTEXITCODE -eq 0) { return @('py', '-3.11') }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $version = & python -c 'import sys; print(sys.version_info[0])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -eq '3') { return @('python') }
    }
    return $null
}

function Invoke-Python {
    param([string[]] $Command, [string[]] $Arguments)
    $exe = $Command[0]
    $argv = @()
    if ($Command.Count -gt 1) { $argv += $Command[1..($Command.Count - 1)] }
    $argv += $Arguments
    & $exe @argv
}

$python = Find-Python
if ($null -eq $python) {
    # 64-bit, and it must match the 64-bit terminal: a 32-bit Python
    # fails the MT5 IPC handshake with an error that says nothing.
    Write-Host 'Installing Python 3.11, 64-bit...'
    $installer = Join-Path $env:TEMP 'python-3.11.exe'
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' `
        -OutFile $installer
    Start-Process -Wait -FilePath $installer -ArgumentList `
        '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0'
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $python = Find-Python
    if ($null -eq $python) {
        throw 'Python installed but is still not on PATH. Close this window, open a new one, and run this script again.'
    }
} else {
    Write-Host ('Using Python: ' + ($python -join ' '))
}

Write-Host 'Installing dependencies...'
Push-Location $root
Invoke-Python $python @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Python $python @('-m', 'pip', 'install', '-r', 'requirements.txt')

if (-not (Test-Path (Join-Path $root 'config.json'))) {
    Copy-Item (Join-Path $root 'config.example.json') (Join-Path $root 'config.json')
    Write-Host 'Created config.json from the example.'
}
if (-not (Test-Path (Join-Path $root '.env'))) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
}

Write-Host 'Running the safety tests...'
Invoke-Python $python @('-m', 'pytest', 'tests', '-q')
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw 'The test suite failed. Do not trade on this build.'
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

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host 'Still to do by hand, once:'
Write-Host '  1. Install MetaTrader 5 TWICE, into separate folders'
Write-Host '     such as C:\MT5-A and C:\MT5-B, and log each into its account.'
Write-Host '  2. Press Algo Trading in each terminal so it turns green.'
Write-Host '  3. Do NOT run the terminals as Administrator. A terminal'
Write-Host '     started elevated will not accept a connection from a'
Write-Host '     normally-started Python.'
Write-Host '  4. Double-click START TRADING on the desktop, then fill in'
Write-Host '     the two accounts on the Exchanges page.'
