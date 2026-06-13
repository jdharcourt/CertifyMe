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

Write-Host "Done. Restart KiCad, then in the PCB Editor use"
Write-Host "  Tools > External Plugins > CertifyMe: Link Datasheets"
Write-Host "(or the toolbar button). Put your DigiKey credentials in a .env"
Write-Host "file in the project folder - see .env.example."
