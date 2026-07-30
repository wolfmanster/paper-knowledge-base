"""
论文知识库 — 非交互式查询（供 Skill 调用）
========================================
用法:
  python query.py "你的问题" [top_k]                   语义搜索（默认）
  python query.py --mode text "关键字" [top_k]         文本搜索（快速）
  python query.py --mode semantic "你的问题" [top_k]   显式语义搜索
  python query.py --local "你的问题" [top_k]           跳过常驻服务，本进程加载模型

输出：JSON 格式搜索结果
"""

import json
import sys
from pathlib import Path

from utils import (
    SCRIPTS_DIR,
    ensure_utf8_stdout,
    get_or_create_chroma_collection,
    get_version,
    load_bi_encoder,
    load_cross_encoder,
    sigmoid,
)

ensure_utf8_stdout()

# 确保能找到 scripts/ 下的模块
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _get_collection():
    """连接 ChromaDB 并获取论文集合，失败时打印错误并退出。"""
    try:
        return get_or_create_chroma_collection()
    except ValueError:
        _fail("Chroma 集合不存在。请运行: python scripts/ingest.py")
    except Exception as e:
        _fail(f"ChromaDB 连接失败: {e}")


def _fail(msg: str):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def get_paper_chunks(
    filename: str = "",
    paper_id: str = "",
    max_chars: int = 8000,
) -> list[dict]:
    """从 ChromaDB 按 filename 或 paper_id 获取论文的全部文本块。

    返回按 chunk_index 排序的块列表，每个块包含 text / section / chunk_index。
    这是读取论文全文的推荐方式——不依赖 PDF 文件是否存在。
    """
    if not filename and not paper_id:
        return []

    collection = _get_collection()

    # 用 where 子句过滤
    where_clause = {}
    if filename:
        where_clause["filename"] = filename
    if paper_id:
        where_clause["paper_id"] = paper_id

    try:
        results = collection.get(
            where=where_clause,
            include=["documents", "metadatas"],
        )
    except Exception:
        return []

    if not results["ids"]:
        return []

    # 组装并排序
    chunks = []
    for _doc_id, doc, meta in zip(
        results["ids"], results["documents"], results["metadatas"]
    ):
        chunks.append({
            "text": doc,
            "section": meta.get("section", ""),
            "chunk_index": meta.get("chunk_index", 0),
        })

    chunks.sort(key=lambda c: c["chunk_index"])

    # 按 max_chars 截断
    total = 0
    truncated = []
    for c in chunks:
        total += len(c["text"])
        if total > max_chars:
            break
        truncated.append(c)

    return truncated


def search_with_components(
    query: str,
    top_k: int,
    bi_encoder,
    cross_encoder,
    collection,
) -> list:
    """使用已加载的模型和集合执行两阶段搜索。"""
    # Bi-Encoder 初检
    query_emb = bi_encoder.encode([query], normalize_embeddings=True).tolist()
    initial_k = max(top_k * 2, 20)
    results = collection.query(
        query_embeddings=query_emb,
        n_results=initial_k,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"] or not results["ids"][0]:
        return []

    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    ids = results["ids"][0]

    # Cross-Encoder 重排
    if cross_encoder is not None:
        pairs = [[query, doc] for doc in documents]
        scores = [sigmoid(float(s)) for s in cross_encoder.predict(pairs)]
        ranked = sorted(
            zip(scores, ids, documents, metadatas), key=lambda x: x[0], reverse=True
        )[:top_k]
    else:
        distances = results["distances"][0]
        ranked = [
            (1.0 - float(distance), i, d, m)
            for distance, i, d, m in zip(distances, ids, documents, metadatas)
        ][:top_k]

    # 格式化输出
    from utils import extract_title_from_filename

    output = []
    for score, doc_id, doc, meta in ranked:
        title = meta.get("title") or extract_title_from_filename(Path(meta.get("filename", "")))
        output.append({
            "score": round(float(score), 4),
            "title": title or "Unknown",
            "section": meta.get("section", ""),
            "filename": meta.get("filename", ""),
            "preview": doc[:300],
        })

    return output


def search(query: str, top_k: int = 5) -> list:
    """在当前进程加载模型并执行两阶段搜索。"""
    try:
        return search_with_components(
            query=query,
            top_k=top_k,
            bi_encoder=load_bi_encoder(),
            cross_encoder=load_cross_encoder(),
            collection=_get_collection(),
        )
    except Exception as e:
        _fail(f"模型加载失败: {e}")


def search_via_service(query: str, top_k: int = 5) -> list:
    """通过常驻语义检索服务搜索，服务未运行时自动拉起。"""
    from semantic_service import remote_search

    return remote_search(query, top_k)


if __name__ == "__main__":
    # ── 参数解析 ──────────────────────────────────────────
    # 用法:
    #   python query.py <查询语句> [top_k]                      语义搜索（默认）
    #   python query.py --mode text <查询语句> [top_k]           文本搜索
    #   python query.py -m text <查询语句> [top_k]
    #   python query.py --mode semantic <查询语句> [top_k]      显式语义搜索
    #   python query.py --local <查询语句> [top_k]             本进程加载模型
    #   python query.py --get-paper-chunks "filename.pdf"       获取论文全文块
    #   python query.py -g "filename.pdf"                       同上（简写）
    #   python query.py --version                               显示版本号

    # --version 快速退出（不延迟导入）
    if "--version" in sys.argv[1:]:
        print(f"paper-knowledge-base {get_version(SCRIPTS_DIR.parent)}")
        sys.exit(0)

    search_mode = "semantic"
    local_semantic = False
    get_chunks_filename = None
    args: list[str] = sys.argv[1:]

    if "--local" in args:
        local_semantic = True
        args.remove("--local")

    # 解析 --get-paper-chunks / -g
    i = 0
    while i < len(args):
        if args[i] in ("--get-paper-chunks", "-g"):
            if i + 1 < len(args):
                get_chunks_filename = args[i + 1]
                del args[i : i + 2]
            else:
                _fail("--get-paper-chunks / -g 需要一个 filename 参数")
            break
        elif args[i] in ("--mode", "-m"):
            if i + 1 < len(args):
                search_mode = args[i + 1].lower()
                if search_mode not in ("text", "semantic"):
                    _fail(f"无效的 --mode 值: {search_mode}，可选: text | semantic")
                del args[i : i + 2]
            else:
                _fail("--mode 需要参数: text | semantic")
            break
        else:
            # 如果第一个参数不是 flag，使用旧的 positional 模式
            break

    # 优先处理 --get-paper-chunks
    if get_chunks_filename:
        chunks = get_paper_chunks(filename=get_chunks_filename)
        full_text = "\n\n".join(
            f"[{c['chunk_index']}] ({c['section']}) {c['text']}"
            for c in chunks
        )
        print(json.dumps({
            "filename": get_chunks_filename,
            "chunk_count": len(chunks),
            "total_chars": len(full_text),
            "full_text": full_text[:100000],  # 安全截断
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if len(args) < 1:
        _fail("用法: python query.py [--mode text|semantic] <查询语句> [top_k]")

    query = args[0]
    if not query or not query.strip():
        _fail("查询内容不能为空。用法: python query.py [--mode text|semantic] <查询语句> [top_k]")

    try:
        top_k = int(args[1]) if len(args) > 1 else 5
    except ValueError:
        _fail("top_k 必须为整数")
    if not 1 <= top_k <= 100:
        _fail("top_k 必须在 1 到 100 之间")

    if search_mode == "text":
        # 文本/关键词搜索
        try:
            from quick_search import search as text_search
        except ImportError as e:
            _fail(
                f"无法加载 quick_search 模块: {e}\n"
                "提示: 请从 paper-knowledge-base/ 目录运行，"
                "或使用根目录的 python pkb.py --mode text ..."
            )

        results = text_search(query, top_k)
    else:
        # 语义搜索
        if local_semantic:
            sys.stderr.write("正在本进程加载语义模型（~530MB，首次约 90 秒）...\n")
            sys.stderr.flush()
            results = search(query, top_k)
        else:
            sys.stderr.write(
                "正在启动语义检索服务后台进程并加载模型"
                "（~530MB，首次约 90 秒）...\n"
            )
            sys.stderr.flush()
            try:
                results = search_via_service(query, top_k)
            except Exception as e:
                _fail(
                    f"常驻语义检索服务不可用: {e}；"
                    "如需本进程搜索，请显式使用 --local"
                )

    print(json.dumps(results, ensure_ascii=False, indent=2))
