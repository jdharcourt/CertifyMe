<#
.SYNOPSIS
    Install the CertifyMe KiCad Action Plugin into your KiCad plugins folder.

.DESCRIPTION
    Copies the kicad_plugin/ files plus the bundled certifyme engine into
    KiCad's 3rd-party plugins directory so the toolbar button appears in the
    PCB Editor. Re-run after pulling updates.

.PARAMETER PluginsDir
    Override the auto-detected KiCad plugins directory.

.EXAMPLE
    ./install_plugin.ps1
    ./install_plugin.ps1 -PluginsDir "D:\KiCadPlugins"
#>
param(
    [string]$PluginsDir
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-KiCadPluginsDir {
    $docs = [Environment]::GetFolderPath("MyDocuments")
    $candidates = @()
    foreach ($ver in @("9.0", "8.0", "7.0")) {
        $candidates += Join-Path $docs "KiCad\$ver\3rdparty\plugins"
    }
    # Fall back to the scripting plugins dir.
    foreach ($ver in @("9.0", "8.0", "7.0")) {
        $candidates += Join-Path $env:APPDATA "kicad\$ver\scripting\plugins"
    }
    foreach ($c in $candidates) {
        $parent = Split-Path -Parent $c
        if (Test-Path $parent) { return $c }
    }
    return $candidates[0]
}

if (-not $PluginsDir) { $PluginsDir = Find-KiCadPluginsDir }
$target = Join-Path $PluginsDir "certifyme"

Write-Host "Installing CertifyMe plugin to: $target"
New-Item -ItemType Directory -Force -Path $target | Out-Null

# Copy plugin files.
Copy-Item -Force (Join-Path $repo "kicad_plugin\__init__.py") $target
Copy-Item -Force (Join-Path $repo "kicad_plugin\action_certifyme.py") $target
$icon = Join-Path $repo "kicad_plugin\icon.png"
if (Test-Path $icon) { Copy-Item -Force $icon $target }

# Bundle the engine as a subpackage so `from .certifyme...` works.
$engineSrc = Join-Path $repo "src\certifyme"
$engineDst = Join-Path $target "certifyme"
if (Test-Path $engineDst) { Remove-Item -Recurse -Force $engineDst }
Copy-Item -Recurse -Force $engineSrc $engineDst

Write-Host "Plugin files installed."
Write-Host ""

# --- Optional: capture DigiKey API keys now -------------------------------
$configDir = Join-Path $env:APPDATA "CertifyMe"
$configFile = Join-Path $configDir "credentials.env"

Write-Host "DigiKey API credentials"
Write-Host "  Get a Client ID and Client Secret at https://developer.digikey.com/"
Write-Host "  (You can also do this later from the plugin dialog or via 'certifyme setup'.)"
$enter = Read-Host "Enter your DigiKey API keys now? (y/N)"

if ($enter -match '^[Yy]') {
    $clientId = Read-Host "DigiKey Client ID"
    $secureSecret = Read-Host "DigiKey Client Secret" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
    try {
        $clientSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $useSandbox = (Read-Host "Use DigiKey sandbox? (y/N)") -match '^[Yy]'

    if ([string]::IsNullOrWhiteSpace($clientId) -or [string]::IsNullOrWhiteSpace($clientSecret)) {
        Write-Warning "Both a Client ID and Secret are required; skipping key save."
    } else {
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null
        $lines = @(
            "# CertifyMe DigiKey API credentials",
            "# Written by install_plugin.ps1. Keep this private.",
            "",
            "DIGIKEY_CLIENT_ID=$clientId",
            "DIGIKEY_CLIENT_SECRET=$clientSecret"
        )
        if ($useSandbox) { $lines += "DIGIKEY_SANDBOX=1" }
        $lines | Set-Content -Path $configFile -Encoding utf8
        Write-Host "Saved credentials to $configFile"
    }
}

Write-Host ""
Write-Host "Done. Restart KiCad, then in the PCB Editor use:"
Write-Host "  Tools > External Plugins > CertifyMe: Link Datasheets  (or the toolbar button)"
Write-Host "You can review/update keys anytime in the plugin's"
Write-Host "'DigiKey API credentials' panel (Save / Test buttons)."
