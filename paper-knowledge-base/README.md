# Paper Knowledge Base

本地论文知识库，基于语义搜索，从 Zotero 经 MinerU pipeline 提取后存入向量库。

## 项目框架

```
paper knowledge base/
├── .claude/skills/paper-search/    # paper-search skill（自动触发搜索）
├── scripts/                        # 所有可执行脚本
│   ├── query.py                    # 查询入口（语义 + 文本，JSON 输出）
│   ├── semantic_service.py         # 常驻语义检索服务（模型只加载一次）
│   ├── index_generation.py         # Chroma 跨进程更新代际标记
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
├── requirements-gpu.txt            # NVIDIA CUDA 12.8 可选依赖
└── .gitignore
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# NVIDIA GPU（CUDA 12.8）
pip install -r requirements-gpu.txt

# 交互式搜索
python scripts/search.py

# 语义查询（JSON 输出；首次自动启动服务并预热，后续复用模型）
python scripts/query.py "your search query" 5

# 可提前预热、查看或停止常驻服务
python scripts/semantic_service.py start
python scripts/semantic_service.py status
python scripts/semantic_service.py stop

# 调试时跳过服务，在当前进程加载模型
python scripts/query.py --local "your search query" 5

# 单次关键词查询（~10ms 响应）
python scripts/query.py --mode text "topology optimization" 10
 
# 更新论文集合描述信息（自动从 ChromaDB 提取关键词和数量）
python scripts/generate_collection_info.py
```

## 搜索系统

```
用户查询
  ├── 语义搜索（默认）  → query.py → 常驻服务 → ChromaDB 初检 → Cross-Encoder 重排 → JSON
  └── 文本搜索（--mode text） → query.py → quick_search.py → kb/index.db (SQLite FTS5) → JSON
```

| 特性 | 语义搜索 | 文本搜索 |
|------|---------|---------|
| 速度 | 首次约 31s 预热，后续复用模型 | ~10ms |
| 适用 | 概念/研究问题 | 关键词/标题/特定术语 |
| 引擎 | ChromaDB HNSW 余弦距离 | SQLite LIKE + FTS5 trigram |
| 重排 | Cross-Encoder（ms-marco-MiniLM） | 无（FTS5 rank 内置） |
| 评分 | 0-1（sigmoid 归一化） | 0-10 |

## 功能

- **语义搜索**：Bi-Encoder 初检 → Cross-Encoder 二次重排 + sigmoid 归一化
- **常驻模型**：`query.py` 自动连接本机 `127.0.0.1:8765`，服务缺失时后台拉起；日志写入 `kb/semantic_service.log`
- **索引自动刷新**：`ingest.py` / `sync_zotero.py` 写入后推进索引代际；下一次查询自动重启服务并加载最新 HNSW
- **GPU 优先**：检测到 CUDA（或 Apple MPS）时两个语义模型自动使用 GPU，否则回退 CPU；NVIDIA 环境使用 `requirements-gpu.txt`，`semantic_service.py status` 的 `device` 字段显示实际设备
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

# 单篇默认最多运行 24 小时；需要时可调整
python scripts/sync_zotero.py --mineru-timeout-hours 48
```

同步保留完整元数据：标题、摘要、DOI、作者、期刊、年份、所属集合（如 "LC/仿生"、"CTP"）。所有同步入口共享跨进程锁，计划任务与手动同步不会并发写入 ChromaDB；超时或提取失败的论文保留到下次同步重试。

### Zotero 新论文自动同步

`watch_zotero.py` 是跨平台监听入口。它监控 `zotero.sqlite` 及其 WAL，默认每天检查一次并自动执行上述增量同步。启动时也会先同步一次，避免关机期间新增的论文遗漏。Zotero 附件在 500 MB 以内时不会因大小被跳过，MinerU 默认提取全部页面。

```bash
# Windows / macOS / Linux 均可直接运行
python scripts/watch_zotero.py

# Zotero 或 MinerU 不在默认位置时
python scripts/watch_zotero.py --zotero-dir /path/to/Zotero --mineru-dir /path/to/MinerU-GUI

# 如需覆盖每天一次的默认频率，可传入秒数
python scripts/watch_zotero.py --interval 3600
```

Windows 可一键安装为当前用户登录后自动运行的后台任务；脚本会从自身位置推导项目路径，因此仓库移动到其他电脑后仍可使用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_zotero_auto_sync.ps1

# 卸载
powershell -ExecutionPolicy Bypass -File scripts/install_zotero_auto_sync.ps1 -Uninstall
```

macOS/Linux 可将同一条 `python scripts/watch_zotero.py` 命令配置到 LaunchAgent、systemd user service 或其他登录启动器。监听日志写入 `kb/watch_zotero.log`，每次实际同步的详细日志写入 `kb/sync_zotero.log`。

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
