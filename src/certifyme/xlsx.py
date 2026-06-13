"""A tiny, dependency-free .xlsx writer.

Just enough of the OOXML spreadsheet format to emit a single styled worksheet:
inline-string text, numbers, clickable ``HYPERLINK`` cells, a bold frozen header
row, an autofilter and per-column widths. Using only the standard library keeps
the tool installable inside KiCad's bundled Python, which has no pip packages.

Style indices (passed as ``Cell.style``):

    0  default
    1  bold (header)
    2  currency, 2 decimals
    3  currency, 4 decimals
    4  bold currency, 2 decimals (totals)
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass

STYLE_DEFAULT = 0
STYLE_BOLD = 1
STYLE_MONEY2 = 2
STYLE_MONEY4 = 3
STYLE_TOTAL = 4


@dataclass
class Cell:
    value: object = None
    style: int = STYLE_DEFAULT
    kind: str = "auto"          # "auto" | "text" | "number" | "hyperlink"
    href: str | None = None     # for kind == "hyperlink"


def text(value, style: int = STYLE_DEFAULT) -> Cell:
    return Cell("" if value is None else str(value), style, "text")


def number(value, style: int = STYLE_DEFAULT) -> Cell:
    return Cell(value, style, "number")


def hyperlink(url: str, label: str, style: int = STYLE_DEFAULT) -> Cell:
    return Cell(label, style, "hyperlink", href=url)


def blank(style: int = STYLE_DEFAULT) -> Cell:
    return Cell("", style, "text")


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _col_letter(idx: int) -> str:
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_xml(ref: str, cell: Cell) -> str:
    s = f' s="{cell.style}"' if cell.style else ""
    if cell.kind == "hyperlink" and cell.href:
        url = cell.href.replace('"', '""')
        label = str(cell.value).replace('"', '""')
        formula = _xml_escape(f'HYPERLINK("{url}","{label}")')
        val = _xml_escape(str(cell.value))
        return f'<c r="{ref}"{s} t="str"><f>{formula}</f><v>{val}</v></c>'
    if cell.kind == "number" or (
        cell.kind == "auto" and isinstance(cell.value, (int, float))
    ):
        if cell.value is None or cell.value == "":
            return f'<c r="{ref}"{s}/>'
        return f'<c r="{ref}"{s}><v>{cell.value}</v></c>'
    # text / inline string
    val = _xml_escape("" if cell.value is None else str(cell.value))
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{val}</t></is></c>'


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
<numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/>
<numFmt numFmtId="165" formatCode="&quot;$&quot;#,##0.0000"/>
</numFmts>
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _workbook_xml(sheet_name: str) -> str:
    name = _xml_escape(sheet_name)[:31] or "Sheet1"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def _sheet_xml(rows: list[list[Cell]], col_widths, freeze_header: bool) -> str:
    n_cols = max((len(r) for r in rows), default=1)
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
    ]
    if freeze_header and rows:
        parts.append(
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
        )
    if col_widths:
        cols = ['<cols>']
        for i, width in enumerate(col_widths):
            cols.append(f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>')
        cols.append('</cols>')
        parts.append("".join(cols))

    parts.append("<sheetData>")
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_col_letter(c)}{r}", cell) for c, cell in enumerate(row))
        parts.append(f'<row r="{r}">{cells}</row>')
    parts.append("</sheetData>")

    if rows and freeze_header:  # autofilter only valid when row 1 is the header
        ref = f"A1:{_col_letter(n_cols - 1)}{len(rows)}"
        parts.append(f'<autoFilter ref="{ref}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def write_xlsx(
    path,
    rows: list[list[Cell]],
    *,
    sheet_name: str = "BOM",
    col_widths=None,
    freeze_header: bool = True,
) -> None:
    """Write *rows* (a list of rows, each a list of :class:`Cell`) to *path*."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows, col_widths, freeze_header))
