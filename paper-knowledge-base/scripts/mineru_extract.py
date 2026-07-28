"""
MinerU PDF 文本提取 — 基于 mineru_api.convert_document()
=======================================================
通过 subprocess 被 sync_zotero.py 调用。

在 MinerU venv 中运行，调用 MinerU GUI 项目的 mineru_api.convert_document()
完成 PDF 解析，输出 JSON 到 stdout。

用法:
  python mineru_extract.py <pdf_path> <output_dir> [--lang en] [--max_pages 20]

环境变量:
  MINERU_GUI_DIR  指向 MinerU GUI 项目目录（例如 /path/to/MinerU/GUI）
                  用于导入 mineru_api。如果未设置，自动推断相对于此脚本的路径。

输出 (JSON to stdout):
  {"status": "ok", "text": "...", "markdown": "...", "pages": 12, "filename": "..."}
  {"status": "error", "message": "..."}
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path


# ── 将 MinerU GUI 加入 sys.path（fallback） ─────────────
# 优先：pip install -e . 已安装到 venv 中，直接 from mineru_api 即可
# fallback：通过环境变量或自动推断路径

try:
    from mineru_api import convert_document
except ImportError:
    _MINERU_GUI_DIR = os.environ.get("MINERU_GUI_DIR")
    if not _MINERU_GUI_DIR:
        # monorepo: scripts/mineru_extract.py → paper-knowledge-base/ → MinerU-GUI/
        _monorepo = Path(__file__).resolve().parent.parent.parent / "MinerU-GUI"
        if _monorepo.exists():
            _MINERU_GUI_DIR = str(_monorepo)
        else:
            # 旧布局回退：scripts/mineru_extract.py → paper-knowledge-base/ → ../MinerU GUI
            _old = Path(__file__).resolve().parent.parent.parent / "MinerU GUI"
            if _old.exists():
                _MINERU_GUI_DIR = str(_old)
    if _MINERU_GUI_DIR:
        sys.path.insert(0, _MINERU_GUI_DIR)
    try:
        from mineru_api import convert_document
    except ImportError as _e:
        raise ImportError(
            f"无法导入 mineru_api。"
            "请先执行: pip install -e . (在 MinerU GUI 目录下)"
            "或设置 MINERU_GUI_DIR 环境变量指向 MinerU GUI 目录。"
        ) from _e


# ── Markdown → Plain text ────────────────────────────────
# 内联实现（此脚本运行在 MinerU venv 中，不依赖知识库项目的 utils.py）


def _md_to_plain_text(md_content: str) -> str:
    """将 Markdown 转换为纯文本，保留段落结构。"""
    text = md_content

    # 移除代码块
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"~~~[\s\S]*?~~~", "", text)

    # 图片引用 → 空
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

    # 链接 [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 移除标题标记 (### 等)，保留标题文字
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 移除粗体/斜体标记
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)

    # 表格：移除 | 和 --- 分隔线，保留单元格内容
    text = re.sub(r"^\|(.+)\|$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^[-:| ]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", " ", text)

    # 水平分割线
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # 移除 html 标签
    text = re.sub(r"<[^>]+>", "", text)

    # 折叠空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _estimate_page_count(md_text: str) -> int:
    """从 Markdown 中估算页数（通过水平分割线）。"""
    return max(1, len(re.findall(r"^---\s*$", md_text, re.MULTILINE)) + 1)


def _error(message: str):
    result = {"status": "error", "message": message}
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1)


# ── 主逻辑 ────────────────────────────────────────────────


def main():
    # stdout 设置为 UTF-8，否则 print(json.dumps) 含希腊/特殊字符时 GBK 会报错
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) < 3:
        _error("用法: mineru_extract.py <pdf_path> <output_dir> [--lang en] [--max_pages 20]")

    pdf_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    # 解析可选参数
    lang = "en"
    max_pages = 20
    i = 3
    while i < len(sys.argv):
        if sys.argv[i] == "--lang" and i + 1 < len(sys.argv):
            lang = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--max_pages" and i + 1 < len(sys.argv):
            max_pages = int(sys.argv[i + 1])
            i += 2
        else:
            i += 1

    # 校验输入
    if not pdf_path.exists():
        _error(f"PDF 文件不存在: {pdf_path}")
    if pdf_path.stat().st_size == 0:
        _error(f"PDF 文件为空: {pdf_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = convert_document(
            file_path=str(pdf_path),
            backend="pipeline",
            lang="en",
            method="auto",
            max_pages=max_pages,
            device="cpu",
            output_dir=str(output_dir / pdf_path.stem),
        )

        if not result.success:
            error_msg = result.error or "未知错误"
            _error(f"MinerU 提取失败: {error_msg}")

        # 读取扁平化后的 markdown 文件
        # convert_document 内部已执行 flatten: 嵌套 MD → out_dir/{file_stem}.md
        md_file = result.output_md
        if md_file is None or not md_file.exists():
            _error(f"MinerU 输出 MD 文件不存在 (pdf={pdf_path.name})")

        md_text = md_file.read_text(encoding="utf-8")

        if not md_text or len(md_text.strip()) < 20:
            _error(f"MinerU 提取的文本过短 (pdf={pdf_path.name})")

        # 成功输出
        result_json = {
            "status": "ok",
            "markdown": md_text,
            "text": _md_to_plain_text(md_text),
            "pages": _estimate_page_count(md_text),
            "filename": pdf_path.name,
        }
        print(json.dumps(result_json, ensure_ascii=False))
        sys.exit(0)

    except ImportError:
        raise  # 模块导入失败直接抛出，不吞掉 traceback
    except Exception as e:
        _error(f"MinerU 提取失败: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
