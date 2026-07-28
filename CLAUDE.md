# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Monorepo: Paper Knowledge Base + MinerU GUI

锂离子电池热管理领域论文知识库（语义搜索 + 文本搜索）+ MinerU PDF 高精度提取桌面 GUI + Python API，两者集成于同一仓库。

## 常用命令

### 论文知识库

```bash
# 安装依赖
cd paper-knowledge-base && pip install -r requirements.txt

# 交互式搜索
python scripts/search.py

# 语义搜索（JSON，供 Skill 调用）
python scripts/query.py "liquid cooling battery thermal management" 5

# 关键词搜索（~10ms）
python scripts/query.py --mode text "topology optimization" 10

# 获取某篇论文全文 chunks
python scripts/query.py --get-paper-chunks "filename.pdf"

# 构建文本搜索索引
python scripts/build_index.py

# Zotero 同步（monorepo 中自动定位 ../MinerU-GUI）
python scripts/sync_zotero.py                                       # 增量同步
python scripts/sync_zotero.py --dry-run                              # 试运行
python scripts/sync_zotero.py --full-rescan                          # 全量重建
MINERU_DIR=/path/MinerU/GUI python scripts/sync_zotero.py           # 手动指定

# 测试（-s 必需：pytest 9.0.2 Win32 capture bug）
python -m pytest tests/ -v -s
python -m pytest tests/test_quick_search.py -v -s                   # 单文件
python -m pytest tests/test_utils.py::test_something -v -s          # 单测试
```

### MinerU GUI

```bat
:: 首次：注册包到 venv
cd MinerU-GUI && python -m pip install -e .
:: 安装 MinerU 引擎（必须 3.x）
pip install "mineru[core]>=3.0.0,<4.0.0"
:: 启动 GUI
start.bat
:: 代码检查
ruff check . && mypy . --ignore-missing-imports
```

## 架构

### 两项目关系

```
MinerU-GUI/                      paper-knowledge-base/
  mineru_api.py (Python API)        sync_zotero.py
  gui/_core.py (run_core)                │
        │                                 │ subprocess（MinerU venv Python）
        └─────────── 调用 ────────────────┘
                                         │
                                    ChromaDB ← 分块 + 嵌入
                                         │
                                    query.py / search.py → 搜索结果
```

- `sync_zotero.py` 通过 subprocess 调用 `MinerU-GUI/.venv/Scripts/python.exe` 运行 `mineru_extract.py`
- monorepo 中默认自动定位 `../MinerU-GUI` 作为 MinerU 目录
- 也支持 `--mineru-dir` 参数或 `MINERU_DIR` 环境变量

### 论文知识库 — 双路搜索

```
用户查询
  ├── 语义搜索（默认） → ChromaDB 向量检索 → Cross-Encoder 重排 → JSON
  └── 文本搜索（--mode text） → quick_search.py → kb/index.db (SQLite FTS5) → JSON
```

| 特性 | 语义搜索 | 文本搜索 |
|------|---------|---------|
| 速度 | ~5s | ~10ms |
| 适用 | 概念/研究问题 | 关键词/标题/特定术语 |
| 引擎 | ChromaDB HNSW 余弦距离 | SQLite LIKE + FTS5 trigram |
| 重排 | Cross-Encoder (ms-marco-MiniLM) | 无（FTS5 rank 内置） |
| 评分 | 0-1 | 0-10 |

### 导入流水线

```
Zotero PDF → MinerU pipeline auto + CPU → clean_text → 段落分割 → 100词分块
  → Bi-Encoder 嵌入（384维） → ChromaDB upsert（含回滚） → build_index.py（FTS5）
```

**双入口**（均写入同一 ChromaDB 集合 `papers`）：
- `sync_zotero.py` — 主入口，读取 Zotero 本地 `zotero.sqlite`，含完整元数据
- `ingest.py` — 从 `Papers/` 目录用 PyMuPDF 快速提取（无 Zotero 元数据）

### MinerU GUI 架构

```
gui.py → setup_ctk() → MainWindow
  → start_batch_conversion() → daemon thread
  → gui._core.run_core(): setup_env() → do_parse() → find_md() → clean_orphaned_images()
  → (optional) VLM image description via ModelSingleton
  → log_viewer 50ms polling + threading.Event done signal
```

核心转换逻辑 `gui/_core.py` 被 GUI 和 `mineru_api.py` 共用，不含 CTk 依赖，通过 `log: Callable[[str], None]` 回调输出日志。

## 重要约定

### 论文知识库

- **所有可执行代码**在 `scripts/`，数据在 `kb/`，配置文件在根目录
- GBK 终端下需 `PYTHONIOENCODING=utf-8` 或 `sys.stdout.reconfigure(encoding="utf-8")`
- Cross-Encoder 加载失败自动降级（用余弦距离代替）
- ChromaDB 写入含自动回滚：失败时清理新写入的 chunks
- MinerU 提取约束：>25MB 跳过，上限 20 页，单篇 100K 字符
- 摘要提取针对 Elsevier/标准英文/中文三种期刊格式做多模式匹配

### MinerU GUI

- 所有文件使用 `from __future__ import annotations`
- `app.py` 是中心常量枢纽（`OUTPUT_DIR`、`LANGUAGES`、`BACKENDS`、`SUPPORTED_EXTENSIONS`）
- 所有 UI 颜色来自 `gui/theme.py` 的 `PALETTE` 单例
- `setup_ctk()` 必须在 `from gui.main_window import MainWindow` **之前**调用
- env vars 是进程全局状态，`convert_document` 非线程安全（用 `multiprocessing` 并发）
- VLM 描述通过 `ModelSingleton` 进程级单例访问，只对 `hybrid-auto-engine` 后端有效

## 已知问题

- `pytest 9.0.2 Win32 capture bug` — 测试必须加 `-s`
- GBK 编码 — Windows 终端默认 GBK，Unicode 字符会报错
- Cross-Encoder 首次使用自动下载 ~84MB

## 模型

| 模型 | 用途 | 大小 |
|------|------|------|
| `paraphrase-multilingual-MiniLM-L12-v2` | Bi-Encoder 嵌入（384维） | ~450MB |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-Encoder 重排 | ~84MB |

均为 BERT 风格编码器，非 LLM，无需 GPU 或 API Key。

## paper-search Skill

位于 `paper-knowledge-base/.claude/skills/paper-search/SKILL.md`，自动触发于电池热管理相关话题（PCM、液冷、热失控、拓扑优化等）。通过 Bash 调用 `query.py` 执行搜索。
