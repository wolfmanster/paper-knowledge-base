# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

# Monorepo: Paper Knowledge Base + MinerU GUI

两个项目集成于同一仓库：
- **paper-knowledge-base/** — 论文知识库（语义搜索 + 文本检索 + Zotero 自动同步）
- **MinerU-GUI/** — MinerU PDF 提取桌面 GUI + Python API

## 常用命令

### 论文知识库 (paper-knowledge-base/)

```bash
# 依赖安装
cd paper-knowledge-base && pip install -r requirements.txt

# 搜索
python scripts/query.py "your query" 5                        # 语义搜索（~5s, JSON）
python scripts/query.py --mode text "topology opt" 10         # 文本关键词（~10ms）
python scripts/query.py --get-paper-chunks "filename.pdf"     # 获取论文全文切片
python scripts/search.py                                       # 交互式 Rich CLI

# 索引与导入
python scripts/build_index.py                                  # 重建 FTS5 文本索引
python scripts/generate_collection_info.py                     # 更新 ChromaDB 集合描述
python scripts/ingest.py                                       # 从 Papers/ 目录快速导入
python scripts/sync_zotero.py                                  # Zotero 增量同步（主入口）
python scripts/sync_zotero.py --dry-run                        # 试运行
python scripts/sync_zotero.py --full-rescan                    # 全量重建（~3h/122篇）
python scripts/sync_zotero.py --full-rescan --skip-build-index # 全量跳过重建索引

# 测试（-s 必需：pytest 9.0.2 Win32 capture bug）
python -m pytest tests/ -v -s
python -m pytest tests/test_quick_search.py -v -s
```

### MinerU GUI (MinerU-GUI/)

```bat
:: 首次注册（用于被 sync_zotero.py subprocess 调用）
cd MinerU-GUI && python -m pip install -e .
pip install "mineru[core]>=3.0.0,<4.0.0"
start.bat

:: 代码检查
ruff check . && mypy . --ignore-missing-imports
```

## 架构

### 两项目协同

```
MinerU-GUI/                       paper-knowledge-base/
  mineru_api.py (Python API)        sync_zotero.py  ──subprocess──► mineru_api
  gui/_core.py (run_core)                                           │
        │                                                     ChromaDB ← 分块 + 嵌入
        │ (subprocess via MinerU venv Python)                      │
        └─────────────────────────────────────────────►   query.py / search.py
```

- `sync_zotero.py` 通过 subprocess 调用 `MinerU-GUI/.venv/Scripts/python.exe` 运行 `mineru_extract.py`
- monorepo 中默认自动定位 `../MinerU-GUI`；也支持 `MINERU_DIR` 环境变量或 `--mineru-dir` 参数

### 知识库 — 双路搜索

```
用户查询
  ├── 语义搜索（默认） → ChromaDB 向量检索（HNSW/余弦） → Cross-Encoder 重排 → JSON
  └── 文本搜索（--mode text） → quick_search.py → kb/index.db (SQLite FTS5 trigram) → JSON
```

| 特性 | 语义搜索 | 文本搜索 |
|------|---------|---------|
| 速度 | ~5s | ~10ms |
| 适用 | 概念/研究问题 | 关键词/标题/特定术语 |
| 引擎 | Bi-Encoder 嵌入 → ChromaDB HNSW | SQLite LIKE + FTS5 trigram |
| 重排 | Cross-Encoder (ms-marco-MiniLM) | 无（FTS5 rank 内置） |
| 评分 | 0-1（sigmoid 归一化） | 0-10 |

### 导入流水线

```
Zotero PDF → MinerU pipeline + CPU → clean_text → 段落分割 → 100词分块
  → Bi-Encoder 嵌入（384维） → ChromaDB upsert（含自动回滚） → build_index.py（FTS5）
```

**双入口**（均写入 `ChromaDB` 集合 `papers`）：
- `sync_zotero.py` — 主入口，读取 Zotero 本地 `zotero.sqlite`，保留完整元数据（标题/作者/DOI/期刊/年份/分类），通过 MinerU 高精度提取
- `ingest.py` — 从 `Papers/` 目录快速导入，PyMuPDF 提取（无 Zotero 元数据）

**回滚机制**：ChromaDB upsert 之前先查询已有 chunks，失败时清理本次写入的 chunks，不影响已有数据。

**MinerU 提取约束**：>25MB 跳过，上限 20 页，单篇 100K 字符。

**摘要提取**（`utils.py`）对三种期刊格式做多模式匹配：
1. 空格字母格式（Elsevier: `A B S T R A C T`）→ `_normalize_spaced_letters` 对照学术词汇表拆分合并
2. 标准英文 `ABSTRACT ... Keywords`
3. 中文 `摘要 ... 关键词`
4. 回退：去除期刊页眉 boilerplate 后取前 500 字符

### MinerU GUI 架构

```
gui.py → setup_ctk() → MainWindow
  → start_batch_conversion() → daemon thread
  → gui._core.run_core(): setup_env() → do_parse() → find_md() → clean_orphaned_images()
  → (optional) VLM image description via ModelSingleton
  → log_viewer 50ms polling + threading.Event done signal
```

核心转换逻辑 `gui/_core.py` 被 GUI 和 `mineru_api.py` 共用，不含 CTk 依赖，通过 `log: Callable[[str], None]` 回调输出日志路由。

## 重要约定

### 论文知识库
- **所有可执行代码**在 `scripts/`，数据在 `kb/`，配置文件在根目录（`requirements.txt`）
- 双模型加载：Bi-Encoder（`paraphrase-multilingual-MiniLM-L12-v2`, ~450MB）+ Cross-Encoder（`ms-marco-MiniLM-L-6-v2`, ~84MB），首次自动从 HuggingFace 下载
- Cross-Encoder 加载失败自动降级（用 1 - 余弦距离代替重排分数）
- ChromaDB 写入含自动回滚（失败时清理本次写入的 chunks）
- PyTorch 依赖：`requirements.txt` 默认使用 CPU 版；如需 GPU 需手动安装 CUDA 版
- `kb/`、`Papers/`、`__pycache__/` 被 `.gitignore` 排除

### MinerU GUI
- 所有文件使用 `from __future__ import annotations`
- `app.py` 是中心常量枢纽（`OUTPUT_DIR`、`LANGUAGES`、`BACKENDS`、`SUPPORTED_EXTENSIONS`）
- 所有 UI 颜色来自 `gui/theme.py` 的 `PALETTE` 单例（frozen dataclass `Palette`）
- `setup_ctk()` 必须在 `from gui.main_window import MainWindow` **之前**调用
- env vars 是进程全局状态，`convert_document` 非线程安全（用 `multiprocessing` 并发）
- VLM 描述通过 `ModelSingleton` 进程级单例访问，仅对 `hybrid-auto-engine` 后端有效

## 已知问题
- `pytest 9.0.2 Win32 capture bug` — 测试必须加 `-s`
- GBK 编码 — Windows 终端默认 GBK，Unicode 字符会报错；用 `PYTHONIOENCODING=utf-8` 或 `sys.stdout.reconfigure(encoding="utf-8")` 解决
- Cross-Encoder 首次使用自动下载 ~84MB
