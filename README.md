# CertifyMe

A KiCad plugin (plus CLI) for PCB certification automation.

The first tool in the kit is the **Datasheet Linker**: it scrubs through a KiCad
project, finds each component, looks up its datasheet online via the
[DigiKey API](https://developer.digikey.com/), and links the URL into the
matching part's `Datasheet` field — across symbols, footprint libraries, the
schematic, and the live board.

## What it does

For every part it finds it:

1. determines a **search key** — an explicit `MPN` field if present, otherwise a
   common manufacturer-part-number field, otherwise the part's `Value`, otherwise
   its name;
2. queries the **DigiKey** keyword-search API for that key and takes the best
   match's datasheet URL;
3. writes the URL into the part's **`Datasheet`** property.

It edits files **surgically** — only the datasheet token changes, the file's
formatting and line endings (LF/CRLF) are preserved — so the git diff stays
clean. Parts that already have a datasheet are skipped unless you pass
`--overwrite`.

Surfaces it covers:

| Surface | Files | How |
|---|---|---|
| Symbols | `*.kicad_sym` | rewrites the `Datasheet` property |
| Footprint libs | `*.kicad_mod` / `*.pretty` | adds a `Datasheet` property (cloned from `Value` geometry) |
| Schematic | `*.kicad_sch` | rewrites `Datasheet` on cached and placed symbols |
| Live board | open PCB in pcbnew | updates footprint `Datasheet` fields via the KiCad API |

## Install as a KiCad plugin

```powershell
# from the repo root
./install_plugin.ps1
```

This copies the plugin and the bundled engine into KiCad's 3rd-party plugins
folder (auto-detected for KiCad 7/8/9; override with `-PluginsDir`). Restart
KiCad, then in the **PCB Editor**:

> **Tools → External Plugins → CertifyMe: Link Datasheets** (or the toolbar button)

A dialog lets you choose the provider, toggle **dry run**, **overwrite**, and
whether to update the live board and/or the project files. Output is logged in
the dialog. **Dry run is on by default** — review the log, then untick it to
write.

## Credentials

The DigiKey API uses OAuth2 client-credentials. Create an app at
<https://developer.digikey.com/>, then copy [`.env.example`](.env.example) to
`.env` in your **project folder**:

```ini
DIGIKEY_CLIENT_ID=your-client-id
DIGIKEY_CLIENT_SECRET=your-client-secret
# DIGIKEY_SANDBOX=1   # use the sandbox host
```

`.env` is gitignored. The plugin and CLI both read it automatically.

## Command-line use

The same engine runs headless — handy for CI or batch jobs:

```bash
pip install -e .

# dry run, verbose
certifyme path/to/kicad/project --dry-run -v

# write links, using the "MPN" field as the search key
certifyme path/to/kicad/project --field MPN

# overwrite existing datasheet links
certifyme path/to/kicad/project --overwrite
```

Offline testing without DigiKey, using a static `{query: url}` map:

```bash
certifyme project --provider dummy --dummy-map map.json
```

## Project layout

```
src/certifyme/           # reusable engine (stdlib only, no runtime deps)
  sexpr.py               #   span-preserving S-expression parser + editor
  kicad.py               #   part discovery + in-place Datasheet writing
  linker.py              #   orchestration + reporting
  cli.py                 #   `certifyme` command
  providers/             #   DigiKey API client + dummy/offline provider
kicad_plugin/            # KiCad Action Plugin wrapper (pcbnew + wx)
  action_certifyme.py    #   toolbar button, dialog, live-board update
  metadata.json          #   KiCad Plugin & Content Manager manifest
install_plugin.ps1       # copies plugin + engine into KiCad's plugins dir
tests/                   # pytest suite (runs fully offline)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The engine has **no runtime dependencies** (standard library only); the plugin
additionally needs `pcbnew` and `wx`, which ship inside KiCad.
