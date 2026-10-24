"""Render a Markdown file to a paginated PDF using reportlab.

Built for the Wahoo Bay documentation set — handles the subset of
Markdown we actually use:

  * ATX headings (# .. ######) with bookmark + outline entries
  * Paragraphs (with inline `code`, **bold**, *italic*)
  * Fenced code blocks (```text)
  * Unordered + ordered lists (one level of nesting)
  * Blockquotes
  * Tables (GFM pipe syntax, single-row header)
  * Horizontal rules

No images (the docs are text-only), no LaTeX math. Footers carry
page numbers and the source filename.

Usage:
    python scripts/md_to_pdf.py <input.md> [output.pdf]

If output is omitted it writes alongside the input with the same
stem and a .pdf extension.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown as md_lib
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageTemplate,
    PageBreak,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.utils import ImageReader


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

_BASE = getSampleStyleSheet()

# Body — slightly bigger leading than reportlab's default so paragraphs breathe.
BODY = ParagraphStyle(
    "body", parent=_BASE["BodyText"],
    fontName="Helvetica", fontSize=10, leading=14,
    spaceBefore=2, spaceAfter=6, alignment=TA_LEFT,
)

# Heading hierarchy. We want H1 to be visually unmistakable so the
# top-of-page bookmark navigation lines up with the structure.
H1 = ParagraphStyle("h1", parent=_BASE["Heading1"],
                    fontName="Helvetica-Bold", fontSize=22, leading=26,
                    spaceBefore=18, spaceAfter=10, textColor=colors.HexColor("#0b2c44"))
H2 = ParagraphStyle("h2", parent=_BASE["Heading2"],
                    fontName="Helvetica-Bold", fontSize=16, leading=20,
                    spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#0b2c44"))
H3 = ParagraphStyle("h3", parent=_BASE["Heading3"],
                    fontName="Helvetica-Bold", fontSize=13, leading=17,
                    spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1f3b4f"))
H4 = ParagraphStyle("h4", parent=_BASE["Heading4"],
                    fontName="Helvetica-Bold", fontSize=11, leading=15,
                    spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1f3b4f"))
H5 = ParagraphStyle("h5", parent=_BASE["Heading5"],
                    fontName="Helvetica-Bold", fontSize=10, leading=13,
                    spaceBefore=8, spaceAfter=2, textColor=colors.HexColor("#1f3b4f"))
H6 = H5
HEADINGS = {1: H1, 2: H2, 3: H3, 4: H4, 5: H5, 6: H6}

# Code (block + inline). Courier so tabs/columns align.
CODE_BLOCK = ParagraphStyle(
    "code_block", parent=_BASE["Code"],
    fontName="Courier", fontSize=8, leading=10,
    leftIndent=14, rightIndent=14,
    backColor=colors.HexColor("#f3f4f6"),
    borderColor=colors.HexColor("#dcdfe3"),
    borderWidth=0.5, borderPadding=6,
    spaceBefore=4, spaceAfter=8,
)

# Blockquote — left rule + indent.
QUOTE = ParagraphStyle(
    "quote", parent=BODY,
    leftIndent=16, rightIndent=8,
    borderColor=colors.HexColor("#7fbf5f"),
    borderPadding=6,
    backColor=colors.HexColor("#f3faef"),
    fontSize=10, leading=14,
    spaceBefore=4, spaceAfter=8,
)

LIST_ITEM = ParagraphStyle(
    "li", parent=BODY,
    leftIndent=18, bulletIndent=4,
    spaceBefore=0, spaceAfter=2,
)

TABLE_HEADER_STYLE = ParagraphStyle(
    "th", parent=BODY,
    fontName="Helvetica-Bold", fontSize=9, leading=11,
    textColor=colors.white,
)
TABLE_CELL_STYLE = ParagraphStyle(
    "td", parent=BODY,
    fontName="Helvetica", fontSize=9, leading=11,
)


# ---------------------------------------------------------------------------
# Inline markdown → reportlab mini-HTML
# ---------------------------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD       = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC     = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK       = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HTML_ESC   = re.compile(r"[&<>]")
_ESC_MAP    = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc(s: str) -> str:
    """Escape & < > for reportlab's mini-HTML. Quotes are fine in body text."""
    return _HTML_ESC.sub(lambda m: _ESC_MAP[m.group(0)], s)


def render_inline(s: str) -> str:
    """Convert a Markdown line into reportlab-flavoured mini-HTML.

    Order matters: code first (so its contents aren't bold/italic parsed),
    then links, then bold, then italic. Bare `<`/`>` survive only inside
    `<font>` etc. — escape elsewhere by chunking around code spans.
    """
    parts: list[str] = []
    last = 0
    for m in _INLINE_CODE.finditer(s):
        # Escape + format the text between code spans.
        parts.append(_format_non_code(s[last:m.start()]))
        # Code spans: monospace + tinted background. reportlab supports
        # <font face="..."> and <font backColor="...">.
        code = _esc(m.group(1))
        parts.append(
            f'<font face="Courier" size="9" color="#b2492a">{code}</font>'
        )
        last = m.end()
    parts.append(_format_non_code(s[last:]))
    return "".join(parts)


def _format_non_code(s: str) -> str:
    """Apply bold, italic, and link formatting to a non-code text run."""
    s = _esc(s)
    s = _LINK.sub(
        lambda m: f'<link href="{m.group(2)}" color="#1559a8">{m.group(1)}</link>',
        s,
    )
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITALIC.sub(r"<i>\1</i>", s)
    return s


# ---------------------------------------------------------------------------
# Block-level parser
# ---------------------------------------------------------------------------
#
# We deliberately do NOT use the `markdown` library for block parsing,
# because we need flowables not HTML. We hand-walk lines instead — the
# subset we use is small enough that this is much simpler than mapping
# markdown's HTML output to reportlab.
#
# (The `markdown` library is still pip-installed as a fallback — left
# imported in case future docs need extension handling.)
_md_lib_loaded = md_lib.Markdown(extensions=["tables", "fenced_code"])


_HEADING   = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_HR        = re.compile(r"^-{3,}\s*$|^\*{3,}\s*$|^_{3,}\s*$")
_FENCE     = re.compile(r"^```(\w*)\s*$")
_BLOCKQ    = re.compile(r"^>\s?(.*)$")
_OL_ITEM   = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_UL_ITEM   = re.compile(r"^\s*[-*]\s+(.*)$")
_PIPE_ROW  = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_SEP  = re.compile(r"^\s*\|?\s*:?-{2,}\:?\s*(\|\s*:?-{2,}\:?\s*)+\|?\s*$")
_IMAGE     = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$")


def parse_blocks(text: str, base_dir: Path) -> list:
    """Walk the markdown line-by-line, yield reportlab flowables.
    `base_dir` is the directory of the source .md so image paths
    (like `![arch](grant_arch.png)`) resolve correctly."""
    lines = text.splitlines()
    flowables: list = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Image-on-its-own-line: ![alt](path)
        m = _IMAGE.match(line)
        if m:
            alt, src = m.group(1), m.group(2)
            img_path = (base_dir / src).resolve()
            if img_path.exists():
                # Scale to fit the body width (6.5") while preserving aspect ratio.
                reader = ImageReader(str(img_path))
                w_px, h_px = reader.getSize()
                max_w = 6.5 * inch
                max_h = 8.5 * inch  # leave room for the caption + margins
                ratio = min(max_w / w_px, max_h / h_px)
                disp_w, disp_h = w_px * ratio, h_px * ratio
                flowables.append(Spacer(1, 4))
                flowables.append(Image(str(img_path), width=disp_w, height=disp_h))
                if alt:
                    flowables.append(Paragraph(
                        f"<i>{render_inline(alt)}</i>",
                        ParagraphStyle("caption", parent=BODY,
                                       fontSize=8.5, textColor=colors.HexColor("#5a6477"),
                                       alignment=1, spaceBefore=2, spaceAfter=8),
                    ))
                else:
                    flowables.append(Spacer(1, 6))
            else:
                # File missing — emit a visible placeholder so the issue surfaces.
                flowables.append(Paragraph(
                    f"<i>[image missing: {render_inline(src)}]</i>", BODY))
            i += 1
            continue

        # Code fence
        m = _FENCE.match(line)
        if m:
            i += 1
            buf: list[str] = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or EOF)
            flowables.append(Preformatted("\n".join(buf), CODE_BLOCK))
            continue

        # Heading
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            style = HEADINGS.get(level, H6)
            # outlineLevel makes the entry appear in the PDF bookmark tree
            p = Paragraph(render_inline(text), style)
            p.outlineLevel = max(0, level - 1)
            flowables.append(p)
            i += 1
            continue

        # Horizontal rule
        if _HR.match(line):
            flowables.append(Spacer(1, 4))
            flowables.append(
                Table([[""]],
                      colWidths=[6.5 * inch],
                      style=TableStyle([
                          ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cfd2d6")),
                      ]))
            )
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # Blockquote (consecutive > lines into one block)
        if _BLOCKQ.match(line):
            buf = []
            while i < len(lines) and _BLOCKQ.match(lines[i]):
                buf.append(_BLOCKQ.match(lines[i]).group(1))
                i += 1
            joined = " ".join(buf).strip()
            flowables.append(Paragraph(render_inline(joined), QUOTE))
            continue

        # Table — header row + separator + data rows
        if _PIPE_ROW.match(line) and i + 1 < len(lines) and _PIPE_SEP.match(lines[i + 1]):
            header = _split_row(line)
            i += 2
            rows = []
            while i < len(lines) and _PIPE_ROW.match(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            flowables.append(_make_table(header, rows))
            continue

        # Ordered list
        if _OL_ITEM.match(line):
            items, i = _collect_list(lines, i, _OL_ITEM)
            for n, item in enumerate(items, start=1):
                flowables.append(Paragraph(
                    render_inline(item),
                    LIST_ITEM,
                    bulletText=f"{n}.",
                ))
            continue

        # Unordered list
        if _UL_ITEM.match(line):
            items, i = _collect_list(lines, i, _UL_ITEM)
            for item in items:
                flowables.append(Paragraph(
                    render_inline(item),
                    LIST_ITEM,
                    bulletText="•",
                ))
            continue

        # Blank line — separator only
        if not line.strip():
            i += 1
            continue

        # Plain paragraph (join consecutive non-blank, non-special lines)
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _looks_special(lines[i]):
            buf.append(lines[i])
            i += 1
        flowables.append(Paragraph(render_inline(" ".join(buf)), BODY))

    return flowables


def _looks_special(line: str) -> bool:
    """Lines that should NOT be glued onto the previous paragraph."""
    return bool(
        _HEADING.match(line) or _FENCE.match(line) or _BLOCKQ.match(line)
        or _PIPE_ROW.match(line) or _UL_ITEM.match(line) or _OL_ITEM.match(line)
        or _HR.match(line) or _IMAGE.match(line)
    )


def _split_row(line: str) -> list[str]:
    """Parse a `| a | b | c |` table row into ['a', 'b', 'c']."""
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _collect_list(lines: list[str], start: int, regex: re.Pattern) -> tuple[list[str], int]:
    """Collect consecutive list items into a flat list. Continuation
    lines (indented under the bullet) get appended to the current item."""
    items: list[str] = []
    i = start
    while i < len(lines):
        m = regex.match(lines[i])
        if m:
            items.append(m.group(m.lastindex))
            i += 1
            continue
        # Continuation if the line is indented and we have an active item
        if items and lines[i].startswith(("    ", "\t")):
            items[-1] += " " + lines[i].strip()
            i += 1
            continue
        break
    return items, i


def _make_table(header: list[str], rows: list[list[str]]) -> Table:
    """Build a reportlab Table. Equal-width columns; wraps long cell text."""
    n_cols = max(len(header), max((len(r) for r in rows), default=len(header)))
    col_w = (6.5 * inch) / n_cols  # 8.5" page - 1" margins each side = 6.5"
    # Pad ragged rows
    def _pad(r):
        return r + [""] * (n_cols - len(r))
    data = [
        [Paragraph(render_inline(c), TABLE_HEADER_STYLE) for c in _pad(header)]
    ] + [
        [Paragraph(render_inline(c), TABLE_CELL_STYLE) for c in _pad(r)]
        for r in rows
    ]
    tbl = Table(data, colWidths=[col_w] * n_cols, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d4a63")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#f7f8fa")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd2d6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Page template (footer with page number + source filename)
# ---------------------------------------------------------------------------

def _make_doc(out_path: Path, title: str, source_label: str) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=1 * inch, rightMargin=1 * inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=title, author="Wahoo Bay project",
        subject=source_label,
    )

    def _draw_footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6a737d"))
        # left = source label
        canvas.drawString(1 * inch, 0.5 * inch, source_label)
        # right = page N
        canvas.drawRightString(
            letter[0] - 1 * inch, 0.5 * inch,
            f"page {_doc.page}",
        )
        canvas.restoreState()

    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height,
        id="body",
    )
    doc.addPageTemplates([
        PageTemplate(id="main", frames=[frame], onPage=_draw_footer)
    ])
    return doc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def md_to_pdf(input_path: Path, output_path: Path) -> None:
    text = input_path.read_text(encoding="utf-8")
    # Title: prefer the first H1 in the doc; fall back to the filename.
    title = input_path.stem
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m and len(m.group(1)) == 1:
            title = m.group(2).strip()
            break

    flowables = parse_blocks(text, base_dir=input_path.parent)
    doc = _make_doc(output_path, title=title, source_label=input_path.name)
    doc.build(flowables)
    print(f"wrote {output_path} ({output_path.stat().st_size} bytes)")


def main() -> None:
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(2)
    in_path = Path(sys.argv[1]).resolve()
    if not in_path.exists():
        sys.exit(f"input not found: {in_path}")
    out_path = (
        Path(sys.argv[2]).resolve() if len(sys.argv) == 3
        else in_path.with_suffix(".pdf")
    )
    md_to_pdf(in_path, out_path)


if __name__ == "__main__":
    main()
