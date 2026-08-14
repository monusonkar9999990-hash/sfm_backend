<#
    Serves the API to a phone on the same WiFi -- without needing administrator
    rights.

    Windows Firewall allows inbound connections per *program*, not per port.
    The only python.exe with an inbound allowance on this machine is the base
    interpreter; the virtualenv's python.exe is a different file on disk, so a
    server started the usual way is invisible to the phone -- its packets are
    dropped and the app reports "the server took too long to answer".

    Running the base interpreter with the venv's packages on PYTHONPATH is the
    same server, from a binary the firewall already trusts. Nothing is opened
    that was not open before.

    Use:
        .\scripts\run-lan.ps1              # 0.0.0.0:8000
        .\scripts\run-lan.ps1 -Port 8080
#>

param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sitePackages = Join-Path $root 'venv\Lib\site-packages'

if (-not (Test-Path $sitePackages)) {
    throw "No virtualenv at $sitePackages -- create it first."
}

# Whatever python.exe the firewall already lets in, whichever version it is.
$allowed = Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like '*ython*' } |
    ForEach-Object { ($_ | Get-NetFirewallApplicationFilter).Program } |
    Where-Object { $_ -and (Test-Path $_) } |
    Select-Object -First 1

$venvPython = Join-Path $root 'venv\Scripts\python.exe'
$python = $venvPython

if ($allowed) {
    # Only if it is the same build -- the venv's compiled extensions
    # (mysqlclient) are built against one exact version.
    $venvVersion = & $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
    $allowedVersion = & $allowed -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
    if ($venvVersion -eq $allowedVersion) {
        $python = $allowed
        $env:PYTHONPATH = $sitePackages
        Write-Host "Serving with the firewall-allowed interpreter: $python"
    } else {
        Write-Host "Allowed interpreter is Python $allowedVersion but the venv is $venvVersion -- using the venv."
    }
}

if ($python -eq $venvPython) {
    Write-Warning @'
Using the venv's own python.exe. If the phone cannot reach this server, add a
rule once, from an *Administrator* PowerShell:

  New-NetFirewallRule -DisplayName "SFM dev API 8000 (private)" `
    -Direction Inbound -Protocol TCP -LocalPort 8000 `
    -Action Allow -Profile Private
'@
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  Phone should point at:  http://${ip}:$Port"
Write-Host "  Check from the phone :  http://${ip}:$Port/api/v1/   (a 404 page means it is reachable)"
Write-Host "  DJANGO_ALLOWED_HOSTS must contain $ip"
Write-Host ""

Push-Location $root
try {
    & $python manage.py runserver "0.0.0.0:$Port"
} finally {
    Pop-Location
}
