# -*- coding: utf-8 -*-
"""试导样本体检：每个 PDF 能提出多少字、是文字版还是扫描件。

用法：python tools/probe_pdf_sample.py
验收：输出每个文件的页数/字符数/判定，最后给汇总。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "question_bank_raw" / "试导样本"

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("缺 pypdf：venv/Scripts/python.exe -m pip install pypdf")


def probe(path: Path) -> dict:
    """判定文字版 / 扫描件。

    ⚠️ 教训（2026-10-21）：只看前几页会误判——政治真题封面是文字层、
    正文 12 页全是图，按"前 3 页平均"算出来 124 字/页，被判成文字版。
    必须**逐页**看：正文页才是要喂切分脚本的部分。
    """
    try:
        r = PdfReader(str(path))
    except Exception as e:
        return {"err": f"打不开：{type(e).__name__} {e}"}
    pages = len(r.pages)
    texts = []
    for p in r.pages:
        try:
            texts.append((p.extract_text() or "").strip())
        except Exception:
            texts.append("")
    n = sum(len(t) for t in texts)
    # 有文字的页：≥100 字符才算数（页眉页脚残留不算）
    with_text = [i + 1 for i, t in enumerate(texts) if len(t) >= 100]
    ratio = len(with_text) / max(pages, 1)
    if ratio >= 0.8:
        kind = "文字版"
    elif ratio == 0:
        kind = "扫描件"
    else:
        kind = f"混合({len(with_text)}/{pages}页有字)"
    return {"pages": pages, "chars": n, "text_pages": len(with_text),
            "ratio": round(ratio, 2), "kind": kind,
            "head": (texts[0] if texts else "")[:200]}


def main():
    if not SRC.is_dir():
        sys.exit(f"样本目录不存在：{SRC}")
    pdfs = sorted(SRC.rglob("*.pdf"))
    print(f"共 {len(pdfs)} 个 PDF\n")
    print(f"{'文件':<42} {'页数':>4} {'总字符':>8} {'有字页':>6}  判定")
    print("-" * 92)
    kinds = {}
    for p in pdfs:
        rel = str(p.relative_to(SRC))
        d = probe(p)
        if "err" in d:
            print(f"{rel:<42} ERROR {d['err']}")
            kinds["ERROR"] = kinds.get("ERROR", 0) + 1
            continue
        print(f"{rel:<42} {d['pages']:>4} {d['chars']:>8} "
              f"{d['text_pages']:>3}/{d['pages']:<3}  {d['kind']}")
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    print("-" * 88)
    print("汇总：", kinds)
    print("\n=== 第一个文件的开头 200 字（看排版是否可直接切分）===")
    d = probe(pdfs[0])
    print(repr(d.get("head", "")))


if __name__ == "__main__":
    main()
