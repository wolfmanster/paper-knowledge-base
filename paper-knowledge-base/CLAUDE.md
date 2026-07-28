# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Paper Knowledge Base

论文知识库，支持语义搜索和文本关键词搜索。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 首次使用：先将 MinerU GUI 注册到其 venv 中
cd ../MinerU-GUI
python -m pip install -e .

# 交互式语义搜索
python scripts/search.py

# 语义搜索（JSON 输出，供 Skill 调用）
python scripts/query.py "your search query" 5

# 获取某篇论文的全文（从 ChromaDB，不需要 PDF 文件）
python scripts/query.py --get-paper-chunks "filename.pdf"

# 文本/关键词快速搜索（标题 + 摘要 + AI 摘要）
python scripts/query.py --mode text "topology optimization" 10

# 构建文本搜索索引（导入新论文后运行）
python scripts/build_index.py

# 更新论文集合描述信息（自动从 ChromaDB 提取关键词和数量）
python scripts/generate_collection_info.py

# 导入新论文（PDF 放入 Papers/ 后，PyMuPDF 快速提取）
python scripts/ingest.py

# 从 Zotero 同步到知识库（monorepo 中自动找到 ../MinerU-GUI）
python scripts/sync_zotero.py                                             # 增量同步（自动定位 MinerU）
python scripts/sync_zotero.py --dry-run                                    # 试运行
python scripts/sync_zotero.py --full-rescan                                # 全量重建（~3h / 116篇）
python scripts/sync_zotero.py --full-rescan --skip-build-index              # 跳过 FTS 重建
MINERU_DIR=/path/to/MinerU/GUI python scripts/sync_zotero.py               # 或手动指定

# 测试（-s 必需：pytest 9.0.2 Win32 capture 模块 bug 的 workaround）
python -m pytest tests/ -v -s
python -m pytest tests/test_quick_search.py -v -s   # 单文件
python -m pytest tests/test_utils.py::test_something -v -s  # 单测试
```

## 架构概览

### 搜索系统：双路并行

```
用户查询
  ├── 语义搜索（默认）  → query.py → ChromaDB 向量检索 → Cross-Encoder 重排 → JSON
  └── 文本搜索（--mode text） → query.py → quick_search.py → kb/index.db (SQLite FTS5) → JSON
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
  → Bi-Encoder 嵌入（paraphrase-multilingual-MiniLM-L12-v2, 384维）
  → ChromaDB upsert（含回滚）→ 自动调用 build_index.py 重建文本索引
```

**双入口**（均写入同一 ChromaDB 集合 `papers`）：
- `sync_zotero.py` — **主入口**，读取 Zotero 本地 `zotero.sqlite` 获取完整元数据（标题/摘要/DOI/作者/分类），调用 `../MinerU-GUI/mineru_api` subprocess 提取 PDF 文本
- `ingest.py` — 从 `Papers/` 目录导入（PyMuPDF 提取，快速但无 Zotero 元数据）

**MinerU 集成要点**：
- MinerU GUI 项目已通过 `pip install -e .` 注册到其 virtualenv（`.venv/Scripts/python.exe`），`mineru_extract.py` 直接 `from mineru_api import convert_document`
- 参数：`backend="pipeline"` + `method="auto"` + `device="cpu"`，默认提取全部页面
- 约 2-3 min/篇（CPU），写入 `kb/mineru_cache/` 作为缓存
- 关键约束：文件 >500MB 跳过；500MB 以内不因大小跳过；单篇默认最长运行 24 小时，失败进入重试队列

### 摘要提取策略（utils.py）

针对不同期刊格式使用多模式匹配：

1. 空格字母格式（Elsevier：`A B S T R A C T`）→ `_normalize_spaced_letters` 对照已知学术词汇表拆分合并
2. 标准英文 `ABSTRACT ... Keywords` 模式
3. 中文 `摘要 ... 关键词` 模式
4. 回退：去除期刊页眉 boilerplate 后取前 500 字符

### 模型

| 模型 | 用途 | 大小 |
|------|------|------|
| `paraphrase-multilingual-MiniLM-L12-v2` | Bi-Encoder 嵌入（索引 + 查询） | ~450MB |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-Encoder 重排（仅语义搜索） | ~84MB |

> Cross-Encoder 加载失败时自动降级：用 1 - cosine_distance 作为分数。

## 项目结构规范

```
paper knowledge base/
├── .claude/skills/paper-search/SKILL.md   # Skill 定义（自动触发搜索）
├── scripts/                               # 所有可执行代码
│   ├── query.py                           # 查询入口（语义 + 文本，JSON 输出）
│   ├── quick_search.py                    # 文本搜索引擎（LIKE + FTS5）
│   ├── build_index.py                     # 构建文本索引（摘要提取 + SQLite）
│   ├── search.py                          # 交互式 Rich CLI
│   ├── ingest.py                          # PDF 导入（Papers/ 入口）
│   ├── sync_zotero.py                     # Zotero 同步主脚本
│   ├── mineru_extract.py                  # MinerU PDF 提取包装器
│   └── utils.py                           # PDF 提取、分块、摘要提取工具
├── tests/
│   ├── test_zotero_sync.py                # Zotero 同步相关测试
│   ├── test_utils.py                      # 工具函数测试
│   └── test_quick_search.py               # 文本搜索测试
├── Papers/                                # 原始 PDF（不提交到 git）
├── kb/                                    # 知识库数据（不提交到 git）
│   ├── chroma/                            # ChromaDB 持久化（HNSW + 余弦距离）
│   ├── index.db                           # SQLite FTS5 文本搜索索引
│   ├── zotero_checkpoint.json             # Zotero 增量同步检查点
│   └── mineru_cache/                      # MinerU 中间输出缓存
├── requirements.txt
└── .gitignore
```

> **原则**：可执行代码 → `scripts/`，数据 → `kb/`，配置文件 → 根目录。

## 已知问题

- **pytest 9.0.2 Win32 capture 模块 bug**：运行测试必须加 `-s` 参数
- **GBK 编码**：在 Windows 终端（默认 GBK）运行脚本时，如果 print/log 含 Unicode 字符（`✓`、`φ`、`∑`、`Δ`），需确保 `PYTHONIOENCODING=utf-8` 或 `sys.stdout.reconfigure(encoding="utf-8")`
- **Cross-Encoder 模型**：首次使用自动下载，建议手动提前装好 cache 避免每次搜索等待

## Available Skills

- **paper-search** — 语义/文本搜索论文库，支持中英文。按配置的关键词自动触发。
