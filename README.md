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

## Step 1 — Get DigiKey API keys

The datasheet lookup uses DigiKey's API, which needs a free key pair:

1. Go to <https://developer.digikey.com/> and sign in (create an account if needed).
2. **Create an Organization**, then **Create a Production App** (or a *Sandbox*
   app to try it out).
3. Subscribe the app to **Product Information V4**.
4. Copy the app's **Client ID** and **Client Secret** — that's all you need.

> Production keys query the real catalog. Sandbox keys hit a test catalog and
> need the *Sandbox* option ticked wherever you enter them.

## Step 2 — Install the plugin

```powershell
# from the repo root, in PowerShell
./install_plugin.ps1
```

This copies the plugin + bundled engine into KiCad's 3rd-party plugins folder
(auto-detected for KiCad 7/8/9; override with `-PluginsDir`). **The installer
then offers to save your API keys for you** — just paste the Client ID and
Secret when prompted (the secret is hidden as you type). You can skip this and
enter them later.

Restart KiCad, then in the **PCB Editor**:

> **Tools → External Plugins → CertifyMe: Link Datasheets** (or the toolbar button)

## Step 3 — Enter / check your keys (three easy ways)

You only need **one** of these — pick whichever you like. All of them write to
the same secure per-user store, so keys are set once and reused everywhere.

**A. In the plugin dialog (no files to edit).**
The dialog has a **DigiKey API credentials** panel at the top:

- Paste your **Client ID** and **Client Secret**, tick **Sandbox** if relevant.
- Click **Test** to verify the keys against the live API.
- Click **Save credentials** to store them. Done — the status line confirms it.

**B. The installer prompt.** If you answered *yes* during `install_plugin.ps1`,
your keys are already saved. Nothing more to do.

**C. The setup wizard (CLI).**

```bash
certifyme setup
```

It asks for your Client ID and Secret (secret input is hidden), saves them, and
runs a live test. Check anytime with:

```bash
certifyme status      # shows where keys load from, masked
```

### Where keys are stored & precedence

Keys live in a private per-user file — `%APPDATA%\CertifyMe\credentials.env` on
Windows, `~/.config/certifyme/credentials.env` elsewhere — written by any of the
methods above. You never have to hand-edit it.

When a lookup runs, credentials are resolved in this order (first wins):

1. **Environment variables** (`DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`) — handy for CI.
2. **Project `.env`** — per-project keys (use `certifyme setup --project .`). Gitignored.
3. **Global config** — the per-user file above (the default for `setup` and the plugin).

Manual editing is still supported — copy [`.env.example`](.env.example) to a
`.env` if you prefer — but it's entirely optional.

## Command-line use

The same engine runs headless — handy for CI or batch jobs:

```bash
pip install -e .

certifyme setup                 # store API keys (interactive)
certifyme status                # show resolved keys (masked)

# link datasheets (these three are equivalent forms of "link"):
certifyme link path/to/project --dry-run -v
certifyme path/to/project --dry-run -v      # 'link' may be omitted

# use the "MPN" field as the search key
certifyme path/to/project --field MPN

# overwrite existing datasheet links
certifyme path/to/project --overwrite
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
  config.py              #   API-key storage / resolution (global + project)
  cli.py                 #   `certifyme` command (setup / status / link)
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
