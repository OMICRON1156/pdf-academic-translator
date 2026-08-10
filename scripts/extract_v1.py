"""阶段1：PyMuPDF 提取半结构化渲染数据，落盘 JSON。
用法: python scripts/extract_v1.py [input.pdf] [output.json]
"""
import json
import sys
import fitz


def flags_to_labels(flags: int) -> list:
    labels = []
    if flags & (1 << 0):
        labels.append("superscript")
    if flags & (1 << 1):
        labels.append("italic")
    if flags & (1 << 2):
        labels.append("serifed")
    if flags & (1 << 3):
        labels.append("monospaced")
    if flags & (1 << 4):
        labels.append("bold")
    return labels


def extract(path: str) -> dict:
    doc = fitz.open(path)
    result = {
        "source_file": doc.metadata.get("title") or path,
        "total_pages": doc.page_count,
        "pages": [],
    }
    for pno in range(doc.page_count):
        page = doc[pno]
        d = page.get_text("dict")
        blocks = []
        for bi, b in enumerate(d["blocks"]):
            if b["type"] != 0:
                continue
            block = {
                "block_id": "p%d-b%d" % (pno + 1, bi),
                "bbox": [round(v, 1) for v in b["bbox"]],
                "lines": [],
            }
            lines = []
            for li, line in enumerate(b["lines"]):
                text = "".join(s["text"] for s in line["spans"])
                sizes = sorted({round(s["size"], 1) for s in line["spans"]})
                fonts = sorted({s["font"] for s in line["spans"]})
                labels = set()
                for s in line["spans"]:
                    labels.update(flags_to_labels(s["flags"]))
                lines.append({
                    "line_id": "%s-l%d" % (block["block_id"], li),
                    "bbox": [round(v, 1) for v in line["bbox"]],
                    "text": text,
                    "sizes": sizes,
                    "fonts": fonts,
                    "labels": sorted(labels),
                })
            lines.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
            block["lines"] = lines
            blocks.append(block)
        blocks.sort(key=lambda bl: (bl["bbox"][1], bl["bbox"][0]))
        image_regions = []
        for info in page.get_image_info(xrefs=True):
            image_regions.append({
                "bbox": [round(v, 1) for v in info["bbox"]],
                "width": info.get("width"),
                "height": info.get("height"),
            })
        result["pages"].append({
            "page_no": pno + 1,
            "page_size": [round(page.rect.width, 1), round(page.rect.height, 1)],
            "rotation": page.rotation,
            "blocks": blocks,
            "image_regions": image_regions,
        })
    return result


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "sample_desktop.pdf")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "sample_extracted.json")
    data = extract(inp)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for p in data["pages"]:
        n_lines = sum(len(b["lines"]) for b in p["blocks"])
        print("page %d: blocks=%d lines=%d images=%d" % (
            p["page_no"], len(p["blocks"]), n_lines, len(p["image_regions"])))
    print("written:", out)


if __name__ == "__main__":
    main()
