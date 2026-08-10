"""阶段5：排版回填（v9）——溢出页压缩行距与段距，脚注放宽底部限制。
用法: python scripts/render_v1.py
"""
import json
import os
import re

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED = os.path.join(BASE, "tmp", "sample_extracted.json")
STRUCTURED = os.path.join(BASE, "tmp", "sample_structured_v3.json")
TRANSLATED = os.path.join(BASE, "tmp", "sample_translated.json")
OUT_PDF = os.path.join(BASE, "tmp", "sample_zh_v7.pdf")

def _find_font(*candidates):
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


FONT_BODY_FILE = _find_font(
    r"C:\Windows\Fonts\NotoSerifSC-Regular.ttf",
    os.path.join(BASE, "assets", "NotoSerifSC-Regular.ttf"),
)
FONT_HEAD_FILE = _find_font(
    r"C:\Windows\Fonts\NotoSerifSC-Black.ttf",
    os.path.join(BASE, "assets", "NotoSerifSC-Black.ttf"),
)
FONT_BODY = "NotoSerifSC"
FONT_HEAD = "NotoSerifSCBlack"

CONTENT_RATIO = 0.75
MARGIN_TOP = 66.0
MARGIN_BOTTOM = 66.0
FOOTNOTE_BOTTOM = 44.0

BODY_SIZE = 11.0
BODY_LEADING = BODY_SIZE * 1.5
LEAD_MIN = BODY_SIZE * 1.2
LEAD_MID = BODY_SIZE * 1.4
HEADING_SIZE = 16.0
HEADING_LEADING = HEADING_SIZE * 1.5
FOOTNOTE_SIZE = 8.0
FOOTNOTE_LEADING = FOOTNOTE_SIZE * 1.5
CAPTION_SIZE = 9.0
CAPTION_LEADING = CAPTION_SIZE * 1.5
QUOTE_INDENT = 24.0
PARA_GAP = 5.0
PARA_GAP_TIGHT = 3.0
HEADING_GAP = 14.0
PAGE_NO_SIZE = 9.0
INDENT_CHARS = 2.0

NO_START = set("，。；：？！、）】》〉」』…—～·％‰　")
NO_END = set("（“‘《【〔〖〈")

pdfmetrics.registerFont(TTFont(FONT_BODY, FONT_BODY_FILE))
pdfmetrics.registerFont(TTFont(FONT_HEAD, FONT_HEAD_FILE))


def split_chars(text, font, size, width):
    piece = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            add = text[i:j]
            if j < n and "\u4e00" <= text[j] <= "\u9fff":
                add = add + text[j]
                j += 1
            trial = piece + add
            if pdfmetrics.stringWidth(trial, font, size) <= width:
                piece = trial
                i = j
                continue
            return piece if piece else add
        trial = piece + ch
        if pdfmetrics.stringWidth(trial, font, size) <= width:
            piece = trial
            i += 1
        else:
            return piece
    return piece


def take_line(text, font, size, width):
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    pieces = re.findall(r"[A-Za-z]+|\d+|\s|[^\sA-Za-z0-9]", text)
    cur = ""
    for piece in pieces:
        trial = cur + piece
        if pdfmetrics.stringWidth(trial, font, size) <= width:
            cur = trial
        else:
            if cur:
                return cur.rstrip()
            return split_chars(piece, font, size, width)
    return cur.rstrip()


def bind_number_spaces(text):
    return re.sub(r"(?<=\d) (?=[\u4e00-\u9fff])", "", text)


def wrap_text(text, font, size, max_width, first_indent=0.0):
    text = bind_number_spaces(text)
    lines = []
    remaining = text.strip()
    width = max_width - first_indent
    while remaining:
        ln = take_line(remaining, font, size, width)
        remaining = remaining[len(ln):].lstrip()
        while remaining and remaining[0] in NO_START:
            ln += remaining[0]
            remaining = remaining[1:]
        while ln and ln[-1] in NO_END:
            remaining = ln[-1] + remaining
            ln = ln[:-1]
        lines.append(ln)
        width = max_width
    return lines


def style_for(kind, body_lead=BODY_LEADING):
    if kind == "heading":
        return FONT_HEAD, HEADING_SIZE, HEADING_LEADING, "center", 0.0, 0.0
    if kind == "footnote":
        return FONT_BODY, FOOTNOTE_SIZE, FOOTNOTE_LEADING, "left", 12.0, FOOTNOTE_SIZE
    if kind == "caption":
        return FONT_BODY, CAPTION_SIZE, CAPTION_LEADING, "center", 0.0, 0.0
    if kind == "quote":
        return FONT_BODY, BODY_SIZE, body_lead, "left", QUOTE_INDENT, 0.0
    return FONT_BODY, BODY_SIZE, body_lead, "left", 0.0, BODY_SIZE * INDENT_CHARS


def split_translated_units(translated):
    mapping = {}
    for unit in translated["units"]:
        zh = unit["zh_text"].strip()
        blocks = unit["blocks"]
        primary0 = blocks[0].split("+")[0]
        if unit["note"] == "cross_page" and "[PB]" in zh:
            before, after = zh.split("[PB]", 1)
            mapping[primary0] = before.strip()
            mapping[blocks[1].split("+")[0]] = after.strip()
        else:
            if unit["note"] == "cross_page" and "[PB]" not in zh:
                print("警告：跨页单元 %s 缺 [PB]，整段落到第一块" % unit["unit_id"])
            mapping[primary0] = zh
        for extra in blocks[0].split("+")[1:]:
            mapping[extra] = ""
        if unit["note"] == "cross_page":
            for extra in blocks[1].split("+")[1:]:
                mapping[extra] = ""
    return mapping


def build_pages(structured, block_text):
    pages = []
    for p in structured["pages"]:
        items = []
        if not p.get("is_blank"):
            for b in p.get("blocks", []):
                if b["kind"] in ("noise", "page_header_footer"):
                    continue
                text = block_text.get(b["block_id"])
                if text:
                    items.append({
                        "kind": b["kind"],
                        "text": text,
                        "no_indent": b.get("note", "") == "continuation_from_prev",
                    })
        pages.append({
            "page_no": p["page_no"],
            "page_number": p.get("page_number"),
            "is_blank": p.get("is_blank", False),
            "items": items,
        })
    return pages


def compute_page_leading(items, max_width, page_h):
    """默认 1.5 倍行距；溢出时逐级压缩正文行距与段间距。返回 (body_lead, para_gap)。"""
    def total_height(lead, pgap):
        h = 0.0
        for item in items:
            kind = item["kind"]
            if kind == "heading":
                font, size, leading, align, indent, fi = style_for(kind)
                n = len(wrap_text(item["text"], font, size, max_width - indent, fi))
                h += n * leading + HEADING_GAP
            else:
                font, size, leading, align, indent, fi = style_for(kind, lead)
                n = len(wrap_text(item["text"], font, size, max_width - indent, fi))
                h += n * leading + pgap
        return h

    stages = [
        (BODY_LEADING, PARA_GAP),
        (LEAD_MID, PARA_GAP),
        (LEAD_MID, PARA_GAP_TIGHT),
        (LEAD_MIN, PARA_GAP_TIGHT),
    ]
    for lead, pgap in stages:
        if total_height(lead, pgap) <= page_h:
            return round(lead, 1), pgap
    body_lines = 0
    non_body_h = 0.0
    n_items = len(items)
    for item in items:
        kind = item["kind"]
        if kind == "heading":
            font, size, leading, align, indent, fi = style_for(kind)
            non_body_h += len(wrap_text(item["text"], font, size, max_width - indent, fi)) * leading + HEADING_GAP
        elif kind in ("body", "quote"):
            font, size, leading, align, indent, fi = style_for(kind, BODY_LEADING)
            body_lines += len(wrap_text(item["text"], font, size, max_width - indent, fi))
        else:
            font, size, leading, align, indent, fi = style_for(kind, BODY_LEADING)
            non_body_h += len(wrap_text(item["text"], font, size, max_width - indent, fi)) * leading
    gaps = PARA_GAP_TIGHT * max(0, n_items - 1)
    if body_lines > 0:
        needed = (page_h - non_body_h - gaps) / body_lines
        lead = max(LEAD_MIN, round(needed, 1))
        return lead, PARA_GAP_TIGHT
    return LEAD_MIN, PARA_GAP_TIGHT


def draw_page(c, page, page_size):
    w, h = page_size
    margin_x = w * (1.0 - CONTENT_RATIO) / 2.0
    max_width = w * CONTENT_RATIO
    page_h = h - MARGIN_TOP - MARGIN_BOTTOM
    body_lead, para_gap = compute_page_leading(page["items"], max_width, page_h)

    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    if not page["is_blank"] and page["page_number"] is not None:
        c.setFont(FONT_BODY, PAGE_NO_SIZE)
        c.drawCentredString(w / 2.0, 30.0, str(page["page_number"]))
    y = h - MARGIN_TOP
    warnings = []
    for item in page["items"]:
        font, size, leading, align, indent, std_first = style_for(item["kind"], body_lead)
        first_indent = 0.0 if item.get("no_indent") else std_first
        if item["kind"] == "heading":
            y -= HEADING_GAP / 2.0
        lines = wrap_text(item["text"], font, size, max_width - indent, first_indent)
        n_lines = len(lines)
        bottom_limit = FOOTNOTE_BOTTOM if item["kind"] == "footnote" else MARGIN_BOTTOM
        for idx, ln in enumerate(lines):
            if y - size < bottom_limit:
                warnings.append("页%d %s 内容溢出" % (page["page_no"], item["kind"]))
                break
            c.setFont(font, size)
            x = margin_x + indent + (first_indent if idx == 0 else 0.0)
            target_w = max_width - indent - (first_indent if idx == 0 else 0.0)
            if align == "center":
                c.drawCentredString(w / 2.0, y, ln)
            else:
                is_last = (idx == n_lines - 1)
                width = pdfmetrics.stringWidth(ln, font, size)
                if not is_last and len(ln) > 1:
                    gap = (target_w - width) / (len(ln) - 1)
                    if gap > 2.0:
                        c.drawString(x, y, ln)
                    else:
                        t = c.beginText(x, y)
                        t.setFont(font, size)
                        t.setCharSpace(gap)
                        t.textOut(ln)
                        c.drawText(t)
                else:
                    c.drawString(x, y, ln)
            y -= leading
        if item["kind"] == "heading":
            y -= HEADING_GAP / 2.0
        else:
            y -= para_gap
    return warnings


def main():
    extracted = json.load(open(EXTRACTED, encoding="utf-8"))
    structured = json.load(open(STRUCTURED, encoding="utf-8"))
    translated = json.load(open(TRANSLATED, encoding="utf-8"))

    size_by_page = {p["page_no"]: (p["page_size"][0], p["page_size"][1]) for p in extracted["pages"]}
    block_text = split_translated_units(translated)
    pages = build_pages(structured, block_text)

    out = canvas.Canvas(OUT_PDF)
    all_warnings = []
    for page in pages:
        w, h = size_by_page.get(page["page_no"], (441.0, 666.0))
        out.setPageSize((w, h))
        all_warnings.extend(draw_page(out, page, (w, h)))
        out.showPage()
    out.save()

    print("源页数=%d 译文PDF页数=%d" % (len(extracted["pages"]), len(pages)))
    for page in pages:
        w, h = size_by_page.get(page["page_no"], (441.0, 666.0))
        bl, pg = compute_page_leading(page["items"], w * CONTENT_RATIO, h - MARGIN_TOP - MARGIN_BOTTOM)
        print("page_no=%d 印刷页码=%s 空白=%s 排版项=%d 行距=%.1f 段距=%.1f" % (
            page["page_no"], page["page_number"], page["is_blank"], len(page["items"]), bl, pg))
    if all_warnings:
        print("=== 排版警告 ===")
        for w in all_warnings:
            print(" -", w)
    else:
        print("=== 无排版警告 ===")
    print("written:", OUT_PDF)


if __name__ == "__main__":
    main()


