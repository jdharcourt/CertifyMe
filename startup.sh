#!/usr/bin/env bash
# CertifyMe setup wizard (macOS / Linux).
#
# The bash counterpart to install_plugin.ps1. It:
#   1. installs the KiCad Action Plugin + bundled engine,
#   2. optionally captures your DigiKey API keys into the global config
#      (~/.config/certifyme/credentials.env), used by every project, and
#   3. optionally tests the connection through KiCad's own Python.
#
# Usage:
#   ./startup.sh                     # full interactive wizard
#   ./startup.sh --plugins-dir DIR   # override the KiCad plugins directory
#   ./startup.sh --no-keys           # install only, skip the credentials step
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

plugins_dir=""
ask_keys=1
for arg in "$@"; do
    case "$arg" in
        --plugins-dir) shift; plugins_dir="${1:-}";;
        --plugins-dir=*) plugins_dir="${arg#*=}";;
        --no-keys) ask_keys=0;;
        -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0;;
    esac
done

# A Python interpreter to drive the engine. Prefer KiCad's bundled one so the
# credential save and the connection test run in the exact interpreter the
# plugin uses (same SSL/cert behaviour); fall back to system python3.
KICAD_PY="/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
if [[ -x "$KICAD_PY" ]]; then PY="$KICAD_PY"; else PY="$(command -v python3 || true)"; fi
[[ -n "$PY" ]] || { echo "error: no python3 found" >&2; exit 1; }

# --- 1. install plugin files + engine -------------------------------------
echo "==> Installing the CertifyMe KiCad plugin"
if [[ -n "$plugins_dir" ]]; then
    "$repo/install_plugin.sh" "$plugins_dir"
else
    "$repo/install_plugin.sh"
fi
echo

# --- 2. DigiKey credentials ------------------------------------------------
if [[ "$ask_keys" -eq 1 ]]; then
    echo "==> DigiKey API credentials"
    echo "    Create a free app at https://developer.digikey.com/ for a"
    echo "    Client ID and Client Secret (OAuth, 'Production' or 'Sandbox')."
    echo "    These save to your global config and apply to every project."
    echo "    (You can also do this later via 'certifyme setup' or the plugin's"
    echo "     Save credentials button.)"
    printf "Enter your DigiKey API keys now? [y/N] "
    read -r reply
    if [[ "$reply" =~ ^[Yy] ]]; then
        printf "DigiKey Client ID: "
        read -r client_id
        printf "DigiKey Client Secret (hidden): "
        read -rs client_secret; echo
        printf "Use the DigiKey sandbox? [y/N] "
        read -r sb_reply
        sandbox="False"; [[ "$sb_reply" =~ ^[Yy] ]] && sandbox="True"

        if [[ -z "$client_id" || -z "$client_secret" ]]; then
            echo "  Both a Client ID and Secret are required; skipping key save."
        else
            # Delegate to the canonical writer so the file format / chmod 600 /
            # config path never drift from the engine. Secrets pass via env,
            # never on the command line.
            CM_ID="$client_id" CM_SECRET="$client_secret" CM_SANDBOX="$sandbox" \
            PYTHONPATH="$repo/src" "$PY" - <<'PYEOF'
import os
from certifyme import config
path = config.save_credentials(
    os.environ["CM_ID"],
    os.environ["CM_SECRET"],
    sandbox=os.environ.get("CM_SANDBOX") == "True",
    scope="global",
)
print(f"  Saved credentials to {path}")
PYEOF
        fi
    fi
    echo

    # --- 3. optional connection test --------------------------------------
    printf "Test the DigiKey connection now? [y/N] "
    read -r test_reply
    if [[ "$test_reply" =~ ^[Yy] ]]; then
        echo "  Testing through KiCad's Python ($PY)..."
        PYTHONPATH="$repo/src" "$PY" - <<'PYEOF' || true
from certifyme import config
from certifyme.providers import build_provider
config.load_into_env()
try:
    provider = build_provider("digikey")
    url = provider.find_datasheet("STM32F103C8T6")
except Exception as exc:
    print(f"  Test failed: {exc}")
    print("  Check the keys and that the app type matches the sandbox setting.")
else:
    if url:
        print(f"  Success! Example lookup returned:\n    {url}")
    else:
        print("  Connected, but no datasheet returned for the test part "
              "(keys look OK).")
PYEOF
    fi
    echo
fi

echo "Done. Restart KiCad, then in the PCB Editor use:"
echo "  Tools > External Plugins > CertifyMe: Link Datasheets  (or the toolbar button)"
echo "Review/update keys anytime via 'certifyme setup' or the plugin's"
echo "'DigiKey API credentials' panel (Save / Test buttons)."
