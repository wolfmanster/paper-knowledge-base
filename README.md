# Paper Knowledge Base + MinerU GUI Monorepo

论文知识库（语义搜索 + 文本检索）+ MinerU PDF 高精度提取 GUI + Python API 集成仓库。

**仓库地址**：https://github.com/wolfmanster/paper-knowledge-base

---

## 📦 目录结构

```
├── paper-knowledge-base/               ← 论文知识库
│   ├── scripts/                        # 所有可执行脚本
│   │   ├── query.py                    # 查询入口（语义 + 文本，JSON 输出）
│   │   ├── search.py                   # 交互式搜索 CLI（Rich 界面）
│   │   ├── quick_search.py             # 文本搜索引擎（SQLite FTS5 trigram）
│   │   ├── build_index.py              # 构建文本搜索索引
│   │   ├── sync_zotero.py              # Zotero 同步主流程（MinerU 提取 PDF）
│   │   ├── mineru_extract.py           # MinerU PDF 提取包装器
│   │   ├── ingest.py                   # Papers/ 目录批量导入（PyMuPDF）
│   │   └── utils.py                    # PDF 提取、分块、摘要、文本清洗工具
│   ├── tests/                          # 测试套件（83 个测试）
│   ├── .claude/skills/paper-search/    # Claude Code 搜索 skill
│   ├── requirements.txt
│   └── CLAUDE.md
│
├── MinerU-GUI/                         ← MinerU PDF 提取引擎
│   ├── mineru_api.py                   # Python API（供外部项目调用）
│   ├── gui.py                          # 桌面 GUI 入口
│   ├── gui/                            # GUI 组件
│   │   ├── _core.py                    # 核心转换逻辑（GUI + API 共用）
│   │   ├── main_window.py              # 主窗口
│   │   ├── worker.py                   # 后台转换线程
│   │   └── theme.py                    # 主题与字体
│   ├── pyproject.toml                  # 包配置
│   ├── requirements.txt
│   └── CLAUDE.md
│
└── .gitignore
```

---

## 🧠 论文知识库（paper-knowledge-base）

### 功能

- **语义搜索** — Bi-Encoder 嵌入 → ChromaDB 向量检索 → Cross-Encoder 重排 + sigmoid 归一化
- **文本快速搜索** — 标题/摘要/关键词检索，~10ms 响应
- **Zotero 自动同步** — 直读 `zotero.sqlite`，保留元数据（标题、作者、DOI、期刊、年份、分类）
- **MinerU 高精度提取** — `pipeline + auto + CPU` 模式，多栏/公式/表格支持好
- **增量同步** — 记录检查点，只处理新增或修改的论文
- **自动回滚** — ChromaDB 写入失败自动清理旧 chunks
- **全文检索** — 从 ChromaDB 获取某篇论文所有 chunks，无需 PDF 文件
- **Claude Code Skill 集成** — 在项目目录下自动触发搜索

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
| 重排 | Cross-Encoder（ms-marco-MiniLM） | 无（FTS5 rank 内置） |
| 评分 | 0-1（sigmoid 归一化） | 0-10 |

---

## 🛠️ MinerU PDF 提取引擎（MinerU-GUI）

### 功能

- **桌面 GUI** — 基于 CustomTkinter，支持拖拽文件、参数配置、实时日志
- **Python API** — `mineru_api.py` 无 GUI 依赖，可在其他项目直接调用
- **多种后端** — `pipeline`（CPU 可用）和 `hybrid-auto-engine`（仅 GPU）
- **多格式输入** — PDF、图片、Office 文档
- **VLM 图片描述** — 混合模式下支持 VLM 自动描述图片（需 GPU）

### 架构

```
GUI (gui.py)                    API (mineru_api.py)
    │                                    │
    └────────────┬───────────────────────┘
                 │
        gui/_core.py (run_core)
                 │
        mineru[core] (do_parse)
```

---

## 🔗 两项目协同

```
MinerU-GUI (PDF 提取引擎)
    │
    │  subprocess 调用（MinerU-GUI/.venv 中的 Python）
    ▼
paper-knowledge-base/scripts/sync_zotero.py
    │
    │  提取的全文 → 分块 → 嵌入
    ▼
ChromaDB 向量数据库 + SQLite FTS5 索引
    │
    │  搜索时
    ▼
query.py / search.py → 结果展示
```

monorepo 中默认自动定位 `../MinerU-GUI`，也支持 `--mineru-dir` 参数或 `MINERU_DIR` 环境变量。

---

## 🚀 快速开始

### 搜索

```bash
cd paper-knowledge-base
pip install -r requirements.txt
python scripts/query.py "your search query" 5
python scripts/search.py                    # 交互式模式
```

### Zotero 导入

```bash
# 注册 MinerU API（仅首次）
cd ../MinerU-GUI
python -m pip install -e .
pip install "mineru[core]>=3.0.0,<4.0.0"
cd ../paper-knowledge-base

python scripts/sync_zotero.py --dry-run        # 试运行
python scripts/sync_zotero.py --full-rescan    # 全量重建
python scripts/sync_zotero.py                  # 日常增量
```

### MinerU GUI 独立使用

```bash
cd MinerU-GUI
pip install -r requirements.txt
pip install "mineru[core]>=3.0.0,<4.0.0"
start.bat
```

或作为 Python API：

```python
import sys
sys.path.insert(0, "/path/to/MinerU-GUI")
from mineru_api import convert_document
result = convert_document("paper.pdf", backend="pipeline", device="cpu")
```

---

## 🧪 测试

```bash
cd paper-knowledge-base
python -m pytest tests/ -v -s    # -s 必需（pytest 9.0.2 Win32 bug）
```

共 **83 个测试**，覆盖 Zotero 同步、工具函数、文本搜索。

---

## 📋 技术栈

| 组件 | 用途 |
|------|------|
| Python 3.10+ | 运行时 |
| ChromaDB | 向量数据库（HNSW 索引） |
| sentence-transformers | Bi-Encoder 嵌入 + Cross-Encoder 重排 |
| SQLite FTS5 | 文本搜索索引（trigram tokenizer） |
| Rich | CLI 交互式界面 |
| PyMuPDF | 快速 PDF 文本提取 |
| MinerU (3.x) | 高精度 PDF 提取引擎 |
| CustomTkinter | 桌面 GUI 框架 |
| PyTorch (2.x) | 深度学习框架（传递依赖） |

### 模型

| 模型 | 用途 | 大小 |
|------|------|------|
| `paraphrase-multilingual-MiniLM-L12-v2` | Bi-Encoder 文本嵌入（384维） | ~450MB |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-Encoder 搜索重排 | ~84MB |

均为 BERT 风格编码器，非 LLM，无需 GPU 或 API Key。

---

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINERU_DIR` | MinerU GUI 目录路径 | `../MinerU-GUI`（monorepo 自动定位） |
| `MINERU_GUI_DIR` | mineru_extract.py 导入 fallback | `$MINERU_DIR` |

---

## ⚠️ 注意事项

- **pytest 需加 `-s`** — pytest 9.0.2 Windows capture 模块 bug
- **GBK 编码** — Windows 终端默认 GBK，含 Unicode 字符时需 `PYTHONIOENCODING=utf-8`
- **MinerU 版本** — 必须使用 3.x（`<4.0.0`），pip 安装时已自动约束
- **首次运行** — 自动从 HuggingFace 下载嵌入模型（~534MB 合计）
- **GPU 加速** — 默认安装 CPU 版 PyTorch。如需 GPU：`pip install torch --index-url https://download.pytorch.org/whl/cu121`
- **`kb/` 和 `Papers/`** — 由 `.gitignore` 排除，不提交到 Git
