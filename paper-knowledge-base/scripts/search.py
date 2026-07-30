"""
学术论文知识库 — 语义搜索 CLI（带 Cross-Encoder 二次重排）
=====================================================
交互式查询界面，支持中英文语义搜索。
流程：Bi-Encoder 初检（ChromaDB）→ Cross-Encoder 重排 → 展示
"""

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from utils import (
    BASE_DIR,
    extract_title_from_filename,
    get_or_create_chroma_collection,
    load_bi_encoder,
    load_cross_encoder,
    sigmoid,
)

INITIAL_K = 20
FINAL_K = 10

console = Console()


def load_collection():
    try:
        return get_or_create_chroma_collection()
    except Exception:
        console.print("[red]错误: 数据库为空或损坏，请先运行 python ingest.py 导入论文[/red]")
        sys.exit(1)


def load_resources():
    """加载两个模型和向量数据库，任何一个模型失败不阻塞整体启动。"""
    with console.status("加载 Bi-Encoder..."):
        try:
            bi_encoder = load_bi_encoder()
        except Exception as e:
            console.print(f"[red]✗ Bi-Encoder 加载失败: {e}[/red]")
            sys.exit(1)
    if bi_encoder is None:
        sys.exit(1)

    with console.status("加载 Cross-Encoder..."):
        cross_encoder = load_cross_encoder()

    with console.status("连接向量数据库..."):
        collection = load_collection()

    status_parts = [
        "[green]✓[/green] Bi-Encoder",
        "[green]✓[/green] Cross-Encoder" if cross_encoder else "[yellow]✗ Cross-Encoder (禁用重排)[/yellow]",
        f"[green]✓[/green] 数据库 {collection.count()} chunks",
    ]
    console.print(" | ".join(status_parts))
    return bi_encoder, cross_encoder, collection


# ── 两阶段搜索 ────────────────────────────────────────────────

def search(
    bi_encoder,
    cross_encoder,
    collection,
    query: str,
    initial_k: int = INITIAL_K,
    final_k: int = FINAL_K,
) -> dict:
    """两阶段搜索：Bi-Encoder 初检 → Cross-Encoder 重排（可选）。"""
    query_emb = bi_encoder.encode(
        [query], normalize_embeddings=True
    ).tolist()
    results = collection.query(
        query_embeddings=query_emb,
        n_results=initial_k,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"] or not results["ids"][0]:
        return results

    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    ids = results["ids"][0]

    if cross_encoder is not None:
        pairs = [[query, doc] for doc in documents]
        scores = cross_encoder.predict(pairs)
        # sigmoid 归一化，确保得分在 [0,1] 区间
        scores = [sigmoid(float(s)) for s in scores]
        ranked = sorted(
            zip(scores, ids, documents, metadatas),
            key=lambda x: x[0],
            reverse=True,
        )[:final_k]
    else:
        # 无 Cross-Encoder 时使用 Bi-Encoder 的余弦距离
        distances = results["distances"][0]
        ranked = sorted(
            zip(distances, ids, documents, metadatas),
            key=lambda x: x[0],
        )[:final_k]
        # 转为 similarity
        ranked = [(1.0 - d, i, doc, meta) for d, i, doc, meta in ranked]

    return {
        "ids": [[r[1] for r in ranked]],
        "documents": [[r[2] for r in ranked]],
        "metadatas": [[r[3] for r in ranked]],
        "scores": [[float(r[0]) for r in ranked]],
    }


# ── 展示 ──────────────────────────────────────────────────────

def display_results(results: dict, query: str):
    if not results["ids"] or not results["ids"][0]:
        console.print("\n[yellow]未找到相关结果。[/yellow]")
        return

    console.print()
    console.rule(f"[bold cyan]搜索结果: {query}[/bold cyan]")
    console.print()

    seen_papers = set()
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        width=min(console.width, 120),
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("论文", style="bold", no_wrap=False, ratio=3)
    table.add_column("章节", style="green", ratio=1)
    label = "重排得分" if "scores" in results else "相似度"
    table.add_column(label, style="yellow", justify="right", width=10)

    scores = results.get("scores", results.get("distances", [[]]))[0]

    for i, (doc_id, doc, meta, score) in enumerate(
        zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            scores,
        )
    ):
        title = meta.get("title", extract_title_from_filename(Path(meta.get("filename", ""))))
        section = meta.get("section", "—")
        paper_key = meta.get("paper_id", doc_id.split("#")[0])

        title_text = (
            Text(title, style="bold white")
            if paper_key not in seen_papers
            else Text("  └ " + title, style="dim")
        )
        seen_papers.add(paper_key)

        table.add_row(str(i + 1), title_text, section[:30], f"{score:.4f}")

    console.print(table)
    console.print()

    console.print("[bold]最佳匹配段落预览:[/bold]")
    best = results["documents"][0][0]
    meta_best = results["metadatas"][0][0]
    preview = best[:400] + ("..." if len(best) > 400 else "")
    panel = Panel(
        preview,
        title=f"[bold]{meta_best.get('title', '')}[/bold] — {meta_best.get('section', '')}",
        border_style="blue",
        width=min(console.width, 120),
    )
    console.print(panel)
    console.print()


# ── 交互循环 ──────────────────────────────────────────────────

def show_stats(collection):
    """显示统计信息，避免一次性加载全量元数据。"""
    count = collection.count()
    # 只取前 5000 条元数据统计 paper_id 去重（避免 OOM）
    sample_size = min(count, 5000)
    all_meta = collection.get(
        include=["metadatas"],
        limit=sample_size,
    )
    paper_ids = set(m["paper_id"] for m in all_meta["metadatas"])
    console.print(f"[green]论文数: ~{len(paper_ids)} (取样 {sample_size}/{count})[/green]")
    console.print(f"[green]文本块数: {count}[/green]")


def interactive_loop(bi_encoder, cross_encoder, collection):
    console.print()
    mode = "Cross-Encoder 重排" if cross_encoder else "Bi-Encoder 检索（降级模式）"
    console.rule(f"[bold green]🔍 论文知识库 — {mode}[/bold green]")
    console.print()

    # 显示集合描述
    info_file = BASE_DIR / "kb" / "collection_info.json"
    if info_file.exists():
        try:
            import json
            info = json.loads(info_file.read_text(encoding="utf-8"))
            desc = info.get("description", "")
            count = info.get("paper_count", 0)
            keywords = info.get("keywords", [])
            if desc:
                console.print(f"[dim]📚 {desc}[/dim]")
            if keywords:
                console.print(f"[dim]  关键词: {', '.join(keywords[:8])}[/dim]")
        except Exception:
            pass
    console.print()
    console.print(
        f"[dim]数据库 {collection.count()} 个文本块 | "
        f"初检 {INITIAL_K} 条 → top-{FINAL_K} | "
        f"输入 'q' 退出 | 'stats' 查看统计[/dim]"
    )
    console.print()

    while True:
        query = Prompt.ask("[bold cyan]🔍 搜索[/bold cyan]").strip()
        if not query:
            continue
        if query.lower() in ("q", "quit", "exit"):
            console.print("[dim]再见![/dim]")
            break
        if query.lower() == "stats":
            show_stats(collection)
            continue

        with console.status("Bi-Encoder → Cross-Encoder 重排..." if cross_encoder else "搜索中..."):
            results = search(bi_encoder, cross_encoder, collection, query)
        display_results(results, query)


def main():
    bi_encoder, cross_encoder, collection = load_resources()
    try:
        interactive_loop(bi_encoder, cross_encoder, collection)
    except KeyboardInterrupt:
        console.print("\n[dim]再见![/dim]")
        sys.exit(0)


if __name__ == "__main__":
    main()
