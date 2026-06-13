#!/usr/bin/env bash
# Install the CertifyMe KiCad Action Plugin into your KiCad plugins folder.
#
# Copies the kicad_plugin/ files plus the bundled certifyme engine into KiCad's
# 3rd-party plugins directory so the toolbar button appears in the PCB Editor.
# Re-run after pulling updates.
#
# Usage:
#   ./install_plugin.sh                 # auto-detect KiCad plugins dir
#   ./install_plugin.sh /path/to/dir    # override plugins dir
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_plugins_dir() {
    # macOS: ~/Documents/KiCad/<ver>/3rdparty/plugins
    # Linux: ~/.local/share/kicad/<ver>/3rdparty/plugins
    local bases=()
    if [[ "$(uname)" == "Darwin" ]]; then
        bases+=("$HOME/Documents/KiCad")
    else
        bases+=("$HOME/.local/share/kicad")
    fi
    for base in "${bases[@]}"; do
        for ver in 9.0 8.0 7.0; do
            if [[ -d "$base/$ver" ]]; then
                echo "$base/$ver/3rdparty/plugins"
                return 0
            fi
        done
    done
    # Fall back to KiCad 9 on macOS.
    echo "$HOME/Documents/KiCad/9.0/3rdparty/plugins"
}

plugins_dir="${1:-$(find_plugins_dir)}"
target="$plugins_dir/certifyme"

echo "Installing CertifyMe plugin to: $target"
mkdir -p "$target"

# Plugin files.
cp -f "$repo/kicad_plugin/__init__.py"        "$target/"
cp -f "$repo/kicad_plugin/action_certifyme.py" "$target/"
[[ -f "$repo/kicad_plugin/icon.png" ]] && cp -f "$repo/kicad_plugin/icon.png" "$target/"

# Bundle the engine as a subpackage so `from .certifyme...` works.
rm -rf "$target/certifyme"
cp -R "$repo/src/certifyme" "$target/certifyme"

echo "Done. Restart KiCad, then in the PCB Editor use:"
echo "  Tools > External Plugins > CertifyMe: Link Datasheets"
echo "(or the toolbar button). Put your DigiKey credentials in a .env"
echo "file in the project folder - see .env.example."
