"""pdf-academic-translator 一键安装脚本。
检查 Python 版本，按 requirements.txt 安装运行依赖，并检查渲染所需中文字体。
用法: python install_deps.py [--check-only]
--check-only 只检查不安装（供确认环境是否就绪）。
"""
import importlib.util
import os
import subprocess
import sys

MIN_PY = (3, 11)
REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
# pip 包名 -> 导入模块名
PACKAGE_MODULE = {
    "PyMuPDF": "fitz",
    "reportlab": "reportlab",
}
BASE = os.path.dirname(os.path.abspath(__file__))
FONTS = {
    "正文思源宋体 Regular": [r"C:\Windows\Fonts\NotoSerifSC-Regular.ttf", os.path.join(BASE, "assets", "NotoSerifSC-Regular.ttf")],
    "标题思源宋体 Heavy": [r"C:\Windows\Fonts\NotoSerifSC-Black.ttf", os.path.join(BASE, "assets", "NotoSerifSC-Black.ttf")],
}


def main():
    print("=== pdf-academic-translator 依赖安装 ===")
    print("Python 版本:", sys.version.split()[0])
    if sys.version_info < MIN_PY:
        print("错误：需要 Python %d.%d 或更高版本。" % MIN_PY)
        sys.exit(1)
    print("Python 版本满足要求。")

    with open(REQUIREMENTS, encoding="utf-8") as f:
        req_lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

    missing = []
    for line in req_lines:
        pkg = line.split("==")[0].strip()
        mod = PACKAGE_MODULE.get(pkg, pkg.lower().replace("-", "_"))
        if importlib.util.find_spec(mod) is None:
            missing.append(line)
    if missing:
        print("缺少依赖：%s" % ", ".join(missing))
        if "--check-only" in sys.argv:
            print("环境未就绪（--check-only 模式，未执行安装）。")
            sys.exit(1)
        print("正在通过 pip 安装 ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS])
        print("依赖安装完成。")
    else:
        print("所有 pip 依赖已就绪：%s" % ", ".join(req_lines))

    ok = True
    for name, candidates in FONTS.items():
        found = [p for p in candidates if os.path.exists(p)]
        if found:
            print("字体已找到：%s -> %s" % (name, found[0]))
        else:
            ok = False
            print("警告：缺少%s（系统字体与 skill assets 均未找到）。请安装该字体，或修改 "
                  "scripts/render_v1.py 的 FONT_BODY_FILE / FONT_HEAD_FILE。" % name)
    print("=== 检查完成%s ===" % ("（有字体缺失）" if not ok else ""))


if __name__ == "__main__":
    main()
