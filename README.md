# CertifyMe

<!-- test line: push check 2026-06-14 -->

A KiCad plugin (plus CLI) for PCB certification automation.

Two tools, one DigiKey-backed engine:

- **Datasheet Linker** — scrubs a KiCad project, finds each component, looks up
  its datasheet online via the [DigiKey API](https://developer.digikey.com/),
  and links the URL into the matching part's `Datasheet` field (symbols,
  footprint libraries, schematic, and the live board).
- **BOM Generator** — reads the schematic, groups and counts parts, prices each
  one via DigiKey, and writes a **priced Excel (.xlsx) + CSV** Bill of Materials
  with part links. See [Generate a priced BOM](#generate-a-priced-bom).
- **Missing-info Highlighter** — outlines footprints on the PCB whose **datasheet**
  couldn't be found (translucent **white**) or whose **price** couldn't be found
  (translucent **cyan**), right in the PCB editor. See
  [Highlight missing info on the PCB](#highlight-missing-info-on-the-pcb).

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

## Generate a priced BOM

The **BOM Generator** turns the project's schematic into a costed parts list.

**In the plugin:** open the dialog (Tools → External Plugins → CertifyMe), make
sure your DigiKey keys are entered, and click **Generate BOM…**. Pick where to
save the `.xlsx`; a matching `.csv` is written alongside it.

**From the CLI:**

```bash
certifyme bom path/to/project                 # writes <project>-BOM.xlsx
certifyme bom path/to/project -o costed.xlsx --csv -v
certifyme bom board.kicad_sch --currency EUR
```

How it works:

1. Reads components from the schematic (`*.kicad_sch`). Multi-unit parts that
   share a reference are counted once; power/no-connect symbols (`#PWR…`) are
   skipped. If there's no schematic, it falls back to the board (`*.kicad_pcb`).
2. Groups identical parts (by **Value + Footprint + MPN**), counts the quantity,
   and lists their references (`R1, R2, …`).
3. Prices each line through DigiKey — pulling **unit price, manufacturer,
   description, stock, supplier P/N, datasheet link and a buy link**.
4. Writes an Excel workbook with a bold frozen-style header, currency
   formatting, clickable links, and a **TOTAL** row (quantity + extended cost).
   Parts marked **DNP** are listed but excluded from the totals.

Columns: `# · References · Qty · Value · Footprint · MPN · Manufacturer ·
Description · Unit Price · Ext. Price · Stock · Supplier P/N · Datasheet ·
Buy Link · DNP`.

The `.xlsx` is written with a small built-in OOXML writer (no `openpyxl` or other
dependency needed), so it works inside KiCad's bundled Python too.

## Highlight missing info on the PCB

Open the plugin dialog in the **PCB Editor** and click **Highlight Missing**. It
scans every footprint on the open board, looks each one up via DigiKey, and
draws a translucent outline around the parts that are missing information:

- **White (30% translucent)** — datasheet could not be found.
- **Cyan (30% translucent)** — price could not be found.
- A part missing both gets both outlines.

The flagged parts are also listed in the dialog (Ref · Value · Missing);
**double-click a row to zoom** straight to that part on the board. **Clear
Highlights** removes the outlines again.

How it's drawn:

- Outlines are rectangles on the **Eco1.User** (datasheet) and **Eco2.User**
  (price) layers, grouped so they can be removed cleanly.
- The translucent white/cyan colours come from setting those two layers to
  `rgba(255,255,255,0.30)` / `rgba(0,255,255,0.30)` in your active KiCad colour
  theme. This works automatically when your board uses an editable (non-built-in)
  colour theme; otherwise the dialog tells you to set those two layer colours
  once in the **Appearance** panel. If the canvas colours don't update
  immediately, reopen the board or re-select the theme in Preferences.

## Project layout

```
src/certifyme/           # reusable engine (stdlib only, no runtime deps)
  sexpr.py               #   span-preserving S-expression parser + editor
  kicad.py               #   part discovery + in-place Datasheet writing
  linker.py              #   datasheet-linking orchestration + reporting
  bom.py                 #   BOM: collect, group, price, write xlsx/csv
  xlsx.py                #   dependency-free .xlsx writer
  highlight.py           #   missing-info classification + outline styles
  kicad_theme.py         #   reversible board colour-theme editing
  config.py              #   API-key storage / resolution (global + project)
  cli.py                 #   `certifyme` command (setup / status / link / bom)
  providers/             #   DigiKey API client (price + datasheet) + dummy
kicad_plugin/            # KiCad Action Plugin wrapper (pcbnew + wx)
  action_certifyme.py    #   toolbar button, dialog, live-board update, BOM,
                         #   PCB missing-info highlighter
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
