"""阶段5b：用翻译结果生成 EPUB3 电子书。
用法: python scripts/epub_v1.py <structured.json> <translated.json> <out.epub> [书名]
"""
import html
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)


def _esc(text):
    return html.escape(str(text), quote=True)


def _chapter_id(index):
    return "chapter_%03d" % index


def _join_segments(parts):
    text = " ".join(part.strip() for part in parts if part.strip())
    return re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)


def build_block_text(translated):
    """把翻译单元映射回文本块；跨页单元合并为完整段落，不再按页拆开。"""
    mapping = {}
    for unit in translated["units"]:
        zh = (unit["zh_text"] or "").strip()
        blocks = unit["blocks"]
        primary0 = blocks[0].split("+")[0]
        if unit["note"] == "cross_page":
            parts = zh.split("[PB]")
            if len(parts) == len(blocks):
                mapping[primary0] = _join_segments(parts)
            else:
                mapping[primary0] = zh.replace("[PB]", "").replace("[PAGEBREAK]", "").strip()
        else:
            mapping[primary0] = zh.replace("[PB]", "").replace("[PAGEBREAK]", "").strip()
        for block_id in blocks:
            for extra in block_id.split("+")[1:]:
                mapping[extra] = ""
        if unit["note"] == "cross_page":
            for block_id in blocks[1:]:
                mapping[block_id.split("+")[0]] = ""
    return mapping


def _make_chapters(structured, block_text):
    chapters = []
    current = None

    def flush():
        nonlocal current
        if current is not None:
            chapters.append(current)
            current = None

    for page in structured["pages"]:
        for block in page.get("blocks", []):
            if block["kind"] in ("noise", "page_header_footer"):
                continue
            text = ((block_text.get(block["block_id"]) or "")
                    .replace("[PB]", "").replace("[PAGEBREAK]", "").strip())
            if not text:
                continue
            if block["kind"] == "heading":
                flush()
                current = {"title": text, "items": []}
            else:
                if current is None:
                    current = {"title": "正文", "items": []}
                current["items"].append({"kind": block["kind"], "text": text})
    flush()
    if not chapters:
        chapters = [{"title": "正文", "items": []}]
    return chapters


def _item_html(item):
    text = _esc(item["text"])
    kind = item["kind"]
    if kind == "quote":
        return "<blockquote><p>%s</p></blockquote>" % text
    if kind == "footnote":
        return '<p class="footnote">%s</p>' % text
    if kind == "caption":
        return '<p class="caption">%s</p>' % text
    return "<p>%s</p>" % text


def _chapter_xhtml(chapter):
    body = "".join(_item_html(item) for item in chapter["items"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">\n'
        "<head><title>%s</title></head>\n<body>\n"
        "<h1>%s</h1>\n%s\n</body>\n</html>\n"
    ) % (_esc(chapter["title"]), _esc(chapter["title"]), body)


def _container_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )


def _content_opf(title, chapters, book_id, modified):
    items = ['    <item id="nav" href="nav.xhtml" '
             'media-type="application/xhtml+xml" properties="nav"/>']
    spine = ['    <itemref idref="nav"/>']
    for i in range(1, len(chapters) + 1):
        cid = _chapter_id(i)
        items.append('    <item id="%s" href="%s.xhtml" '
                     'media-type="application/xhtml+xml"/>' % (cid, cid))
        spine.append('    <itemref idref="%s"/>' % cid)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="bookid">%s</dc:identifier>\n'
        "    <dc:title>%s</dc:title>\n"
        "    <dc:language>zh-CN</dc:language>\n"
        '    <meta property="dcterms:modified">%s</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n%s\n  </manifest>\n"
        "  <spine>\n%s\n  </spine>\n"
        "</package>\n"
    ) % (book_id, _esc(title), modified, "\n".join(items), "\n".join(spine))


def _nav_xhtml(chapters):
    links = []
    for i, chapter in enumerate(chapters, 1):
        cid = _chapter_id(i)
        links.append('      <li><a href="%s.xhtml">%s</a></li>'
                     % (cid, _esc(chapter["title"])))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">\n'
        "<head><title>目录</title></head>\n<body>\n"
        '<nav epub:type="toc" id="toc">\n'
        "  <h1>目录</h1>\n  <ol>\n%s\n  </ol>\n"
        "</nav>\n</body>\n</html>\n" % "\n".join(links)
    )


def build_epub(structured, block_text, title, epub_path):
    """用结构化页序和译文块映射生成 EPUB3，返回 (epub_path, 章节数)。"""
    chapters = _make_chapters(structured, block_text)
    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, title))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs(os.path.dirname(epub_path), exist_ok=True)
    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _container_xml())
        zf.writestr("OEBPS/content.opf",
                    _content_opf(title, chapters, book_id, modified))
        zf.writestr("OEBPS/nav.xhtml", _nav_xhtml(chapters))
        for i, chapter in enumerate(chapters, 1):
            zf.writestr("OEBPS/%s.xhtml" % _chapter_id(i),
                        _chapter_xhtml(chapter))
    return epub_path, len(chapters)


def main():
    if len(sys.argv) < 4:
        print("用法: python scripts/epub_v1.py <structured.json> "
              "<translated.json> <out.epub> [书名]")
        sys.exit(1)
    structured = json.load(open(sys.argv[1], encoding="utf-8-sig"))
    translated = json.load(open(sys.argv[2], encoding="utf-8-sig"))
    out = sys.argv[3]
    title = sys.argv[4] if len(sys.argv) > 4 else os.path.splitext(os.path.basename(out))[0]
    block_text = build_block_text(translated)
    path, count = build_epub(structured, block_text, title, out)
    print("written:", path)
    print("章节数:", count)


if __name__ == "__main__":
    main()
