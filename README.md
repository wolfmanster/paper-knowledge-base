# Paper Knowledge Base + MinerU GUI Monorepo

锂离子电池热管理领域论文知识库语义搜索系统 + MinerU PDF 高精度提取工具链，两者集成于同一仓库。

**仓库地址**：https://github.com/wolfmanster/paper-knowledge-base

---

## 📦 仓库结构

```
paper-knowledge-base-monorepo/
│
├── paper-knowledge-base/               ← 论文知识库
│   ├── scripts/                        # 所有可执行脚本
│   │   ├── query.py                    # 查询入口（语义 + 文本，JSON 输出）
│   │   ├── search.py                   # 交互式搜索 CLI（Rich 界面）
│   │   ├── quick_search.py             # 文本搜索引擎（SQLite FTS5 trigram）
│   │   ├── build_index.py              # 构建文本搜索索引
│   │   ├── sync_zotero.py              # Zotero 同步主流程（MinerU 提取 PDF）
│   │   ├── mineru_extract.py           # MinerU PDF 提取包装器
│   │   ├── ingest.py                   # Papers/ 目录批量导入（PyMuPDF）
│   │   └── utils.py                    # PDF 提取、分块、摘要、切片工具
│   ├── tests/                          # 测试套件（83 个测试）
│   │   ├── test_quick_search.py
│   │   ├── test_utils.py
│   │   └── test_zotero_sync.py
│   ├── .claude/skills/paper-search/    # Claude Code 自动搜索 skill
│   ├── requirements.txt
│   └── CLAUDE.md
│
├── MinerU-GUI/                         ← MinerU PDF 提取引擎
│   ├── mineru_api.py                   # Python API（供外部项目调用）
│   ├── gui.py                          # 桌面 GUI 入口
│   ├── app.py                          # 常量与工具函数
│   ├── gui/
│   │   ├── _core.py                    # 核心转换逻辑（GUI + API 共用）
│   │   ├── main_window.py              # 主窗口
│   │   ├── worker.py                   # 后台转换线程
│   │   ├── theme.py                    # 主题与字体
│   │   └── widgets/                    # UI 组件
│   ├── pyproject.toml                  # 包配置
│   ├── requirements.txt
│   └── CLAUDE.md
│
├── .gitignore                          # Git 忽略规则
└── README.md                           ← 本文件
```

---

## 🧠 论文知识库（paper-knowledge-base）

### 概览

本地论文知识库，专注于**锂离子电池热管理（BTMS）**领域，收录 **116 篇**学术论文。全部从 Zotero 经 MinerU pipeline 提取全文后存入 ChromaDB 向量数据库，支持语义搜索和关键词搜索。

**覆盖的研究主题**：相变材料（PCM）冷却、液冷板系统、热管冷却、浸没式冷却、热失控与热安全、拓扑优化设计、COMSOL 仿真模拟、多物理场耦合、锂离子电池热特性建模。

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

### 功能

- **语义搜索** — Bi-Encoder 初检 → Cross-Encoder 二次重排 + sigmoid 归一化
- **文本快速搜索** — 标题、摘要、AI 摘要关键词检索，~10ms 响应
- **Zotero 自动同步** — 直读 `zotero.sqlite`，保留 DOI、作者、期刊、年份、集合分类
- **MinerU 高精度提取** — `pipeline + auto + CPU` 模式，多栏/公式/表格支持好
- **增量同步** — 记录检查点，只处理新增或修改的论文
- **自动回滚** — ChromaDB 写入失败自动清理旧 chunks
- **全文检索** — 从 ChromaDB 获取某篇论文所有 chunks，无需 PDF 文件
- **Claude Code Skill 集成** — 在项目目录下自动触发电池热管理相关搜索

---

## 🛠️ MinerU PDF 提取引擎（MinerU-GUI）

### 概览

[`mineru`](https://pypi.org/project/mineru/)（Magic-PDF）的桌面 GUI 封装 + Python API。核心转换逻辑（`gui/_core.py`）同时被 GUI 和 API 共用，无重复代码。

### 功能

- **桌面 GUI** — 基于 CustomTkinter，支持拖拽文件、参数配置、实时日志
- **Python API** — `mineru_api.py` 无 GUI 依赖，可直接在知识库中调用
- **多种后端** — `pipeline`（CPU 可用）和 `hybrid-auto-engine`（仅 GPU）
- **多格式输入** — PDF、图片、Office 文档
- **VLM 图片描述** — 混合模式下支持 VLM 自动描述图片（需 GPU）
- **显存虚拟化** — CPU 模式下自动设置虚拟显存参数

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

## 🔗 两个项目如何协同

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

**交互方式**：
- `sync_zotero.py` 通过 subprocess 调用 `mineru_extract.py`，运行在 `MinerU-GUI/.venv` 环境中
- monorepo 中默认自动定位 `../MinerU-GUI` 作为 MinerU 目录，无需额外配置
- 也支持通过 `--mineru-dir` 参数或 `MINERU_DIR` 环境变量指定路径

---

## 🚀 快速开始

### 搜索（无需 MinerU）

```bash
# 1. 进入知识库目录
cd paper-knowledge-base

# 2. 安装依赖
pip install -r requirements.txt

# 3. 立刻搜索
python scripts/query.py "battery thermal management" 5

# 4. 交互式模式
python scripts/search.py
```

### PDF 提取 + Zotero 导入（需要 MinerU）

```bash
# 1. 注册 MinerU API（仅首次）
cd ../MinerU-GUI
python -m pip install -e .
pip install "mineru[core]>=3.0.0,<4.0.0"
cd ../paper-knowledge-base

# 2. 试运行（查看哪些论文会导入）
python scripts/sync_zotero.py --dry-run

# 3. 全量导入（~3 小时 / 116 篇）
python scripts/sync_zotero.py --full-rescan

# 4. 日常增量同步
python scripts/sync_zotero.py
```

> ⚠️ Zotero 同步前请先关闭 Zotero（SQLite 写锁）。

### MinerU GUI 独立使用

```bash
cd MinerU-GUI
pip install -r requirements.txt
pip install "mineru[core]>=3.0.0,<4.0.0"
start.bat
```

或作为 Python API 调用：

```python
import sys
sys.path.insert(0, "/path/to/MinerU-GUI")
from mineru_api import convert_document

result = convert_document("paper.pdf", backend="pipeline", device="cpu")
if result.success:
    print(result.output_md.read_text())
```

---

## 🧪 测试

```bash
cd paper-knowledge-base
python -m pytest tests/ -v -s    # -s 必需（pytest 9.0.2 Win32 bug）
```

共 **83 个测试**，覆盖 Zotero 同步、工具函数、文本搜索。测试无需 PDF 文件或 MinerU。

---

## 📋 技术栈

| 组件 | 用途 | 项目 |
|------|------|------|
| Python 3.10+ | 运行时 | 通用 |
| ChromaDB | 向量数据库（HNSW 索引） | 知识库 |
| sentence-transformers | Bi-Encoder 嵌入 + Cross-Encoder 重排 | 知识库 |
| SQLite FTS5 | 文本搜索索引（trigram tokenizer） | 知识库 |
| Rich | CLI 交互式界面 | 知识库 |
| PyMuPDF | 快速 PDF 文本提取（ingest.py） | 知识库 |
| MinerU (3.x) | 高精度 PDF 提取引擎 | MinerU-GUI |
| CustomTkinter | 桌面 GUI 框架 | MinerU-GUI |
| PyTorch (2.x) | 深度学习框架（传递依赖） | 通用 |

### 模型

| 模型 | 用途 | 参数量 | 大小 |
|------|------|--------|------|
| `paraphrase-multilingual-MiniLM-L12-v2` | Bi-Encoder 文本嵌入 | 117M | ~450MB |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-Encoder 搜索重排 | 85M | ~84MB |

> 两者均为 BERT 风格的 Transformer 编码器，非 LLM，无需 GPU 也无需 API Key。
> Cross-Encoder 加载失败时自动降级为 Bi-Encoder 评分。

---

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINERU_DIR` | MinerU GUI 目录路径 | `../MinerU-GUI`（monorepo 自动定位） |
| `MINERU_GUI_DIR` | mineru_extract.py 导入 mineru_api 的 fallback 路径 | `$MINERU_DIR` |

---

## ⚠️ 注意事项

- **pytest 需加 `-s`** — pytest 9.0.2 Windows capture 模块 bug
- **GBK 编码** — Windows 终端默认 GBK，含 `✓`、`φ`、`∑` 等 Unicode 字符时需 `PYTHONIOENCODING=utf-8`
- **MinerU 版本** — 必须使用 3.x（`<4.0.0`），pip 安装时已自动约束
- **首次运行** — 自动从 HuggingFace 下载嵌入模型（~534MB 合计）
- **GPU 加速** — 默认安装 CPU 版 PyTorch。如需 GPU 加速：`pip install torch --index-url https://download.pytorch.org/whl/cu121`，然后重新装其他依赖
- **`kb/` 和 `Papers/`** — 由 `.gitignore` 排除，不提交到 Git。新机器上需拷贝或重新导入
