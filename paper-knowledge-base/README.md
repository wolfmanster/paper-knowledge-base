# Paper Knowledge Base

本地论文知识库，基于语义搜索，从 Zotero 经 MinerU pipeline 提取后存入向量库。

## 项目框架

```
paper knowledge base/
├── .claude/skills/paper-search/    # paper-search skill（自动触发搜索）
├── scripts/                        # 所有可执行脚本
│   ├── query.py                    # 查询入口（语义 + 文本，JSON 输出）
│   ├── quick_search.py             # 文本搜索引擎（SQLite FTS5）
│   ├── build_index.py              # 构建文本索引
│   ├── search.py                   # 交互式搜索 CLI（Rich）
│   ├── ingest.py                   # 从 Papers/ 批量导入（PyMuPDF，旧方式）
│   ├── sync_zotero.py              # 主流程：从 Zotero SQLite 同步（MinerU 提取）
│   ├── mineru_extract.py           # MinerU PDF 提取包装器（subprocess 调用）
│   └── utils.py                    # PDF 提取、分块、摘要提取工具
├── tests/
│   ├── test_zotero_sync.py         # Zotero 同步相关测试
│   ├── test_utils.py               # 工具函数测试
│   └── test_quick_search.py        # 文本搜索测试
├── Papers/                         # 原始论文 PDF（可选，不提交）
├── kb/                             # 知识库数据（不提交）
│   ├── chroma/                     # ChromaDB 向量数据库（HNSW + 余弦距离）
│   ├── index.db                    # SQLite FTS5 文本索引（~10ms 搜索）
│   ├── zotero_checkpoint.json      # Zotero 增量同步检查点
│   └── mineru_cache/               # MinerU 临时输出缓存
├── requirements.txt
└── .gitignore
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 交互式搜索
python scripts/search.py

# 单次语义查询（JSON 输出，含 Cross-Encoder 重排）
python scripts/query.py "your search query" 5

# 单次关键词查询（~10ms 响应）
python scripts/query.py --mode text "topology optimization" 10
```

## 搜索系统

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
| 重排 | Cross-Encoder（ms-marco-MiniLM） | 无（FTS5 rank 内置） |
| 评分 | 0-1（sigmoid 归一化） | 0-10 |

## 功能

- **语义搜索**：Bi-Encoder 初检 → Cross-Encoder 二次重排 + sigmoid 归一化
- **文本快速搜索**：标题/摘要/AI 摘要关键词检索，~10ms 响应
- **Zotero 自动同步**：直读 `zotero.sqlite`，保留 DOI、作者、期刊、年份、集合分类
- **MinerU 高精度提取**：`pipeline + auto + CPU` 模式，多栏/公式/表格支持好
- **增量同步**：记录检查点，只处理新增或修改的论文
- **自动回滚**：ChromaDB 写入失败自动清理旧 chunks
- **Skill 集成**：Claude Code 自动触发搜索（需在项目目录下启动）

## 导入论文

### 方式一：从 Zotero 同步（推荐）

```bash
# 先在 MinerU venv 中注册包（仅首次）
cd /path/to/MinerU/GUI
python -m pip install -e .

# 试运行（查看哪些论文会导入）
python scripts/sync_zotero.py --mineru-dir /path/to/MinerU/GUI --dry-run

# 全量重建
python scripts/sync_zotero.py --mineru-dir /path/to/MinerU/GUI --full-rescan
python scripts/sync_zotero.py --mineru-dir /path/to/MinerU/GUI --full-rescan --skip-build-index  # 跳过 FTS 重建

# 日常增量
python scripts/sync_zotero.py --mineru-dir /path/to/MinerU/GUI
```

同步保留完整元数据：标题、摘要、DOI、作者、期刊、年份、所属集合（如 "LC/仿生"、"CTP"）。

### 方式二：手动放入 Papers/（旧方式）

```bash
python scripts/ingest.py
```

此方式用 PyMuPDF 提取，速度较快但无 Zotero 元数据。

## 技术栈

| 组件 | 用途 |
|------|------|
| ChromaDB | 向量数据库（本地持久化） |
| sentence-transformers | Bi-Encoder（嵌入）+ Cross-Encoder（重排） |
| MinerU | PDF 文本提取（pipeline + auto + CPU） |
| SQLite FTS5 | 文本搜索索引 |
| Rich | CLI 交互式界面 |

## 注意事项

- **pytest 需加 `-s`**：pytest 9.0.2 Windows capture 模块 bug
- **GBK 编码**：Windows 终端默认 GBK，含 `✓`、`φ`、`∑` 等 Unicode 字符时需 `PYTHONIOENCODING=utf-8`
- **MinerU 路径**：通过 `--mineru-dir` 参数或 `MINERU_DIR` 环境变量指定，无内置默认路径
- **每次更新 MinerU 代码后**：重新 `pip install -e .`，否则 `mineru_api` 不会反映最新改动
- **Zotero 同步**：需关闭 Zotero 后再运行（SQLite 写锁）
- **首次运行**：自动从 HuggingFace 下载嵌入模型（~450MB encode + ~84MB Cross-Encoder）
- **所有脚本放 `scripts/`**，根目录只放配置文件
- **`kb/`** 由 `.gitignore` 排除，不提交到 Git
