# Paper Knowledge Base + MinerU GUI Monorepo

论文知识库 | 锂离子电池热管理领域语义搜索 + MinerU PDF 提取工具链

## Quickstart — 最短可运行路径

下面是从零开始到能运行一次查询的最短步骤（Unix / macOS 示例）。Windows 等价命令见注释。

```bash
# 1) 克隆仓库并进入目录
git clone https://github.com/wolfmanster/paper-knowledge-base
cd paper-knowledge-base

# 2) 创建并激活虚拟环境（示例：Unix / macOS）
python -m venv .venv
source .venv/bin/activate
# Windows (PowerShell): .venv\Scripts\Activate.ps1

# 3) 安装知识库依赖（仅核心依赖，避免 GPU 专用包）
pip install -r paper-knowledge-base/requirements.txt

# 4) 可选：配置 MinerU（仅当要使用 PDF 提取时）
#  - 指定 MinerU GUI 目录（包含 mineru_api）
export MINERU_DIR=/path/to/MinerU-GUI
# Windows (PowerShell): setx MINERU_DIR "C:\path\to\MinerU-GUI"

# 5) 测试搜索（无需 MinerU）
python paper-knowledge-base/scripts/query.py "battery thermal management" 5

# 6) 试运行 Zotero 同步（如果已配置 MinerU 与 Zotero）
python paper-knowledge-base/scripts/sync_zotero.py --dry-run --mineru-dir /path/to/MinerU-GUI
```

> 说明：首次运行时会从 HuggingFace 下载嵌入模型（约 450MB），请确保网络通畅。

## 目录结构

```text
├── paper-knowledge-base/       # 论文知识库（检索、导入、同步）
│   ├── scripts/                # 可执行代码
│   ├── tests/                  # 测试
│   └── requirements.txt        # 依赖
├── MinerU-GUI/                 # MinerU PDF 提取引擎
│   ├── mineru_api.py           # Python API
│   ├── gui/                    # GUI + 核心逻辑
│   └── app.py                  # 桌面应用入口
└── .gitignore
```

## 详细文档
更多细节见子目录 README：paper-knowledge-base/README.md 和 MinerU-GUI/README.md
