# Paper Knowledge Base + MinerU GUI Monorepo

论文知识库 | 锂离子电池热管理领域语义搜索 + MinerU PDF 提取工具链

## 目录结构

```
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

## 快速开始

```bash
# 1. 安装知识库依赖
cd paper-knowledge-base
pip install -r requirements.txt

# 2. 注册 MinerU API（首次使用）
cd ../MinerU-GUI
python -m pip install -e .
cd ../paper-knowledge-base

# 3. 搜索（无需 MinerU）
python scripts/query.py "battery thermal management" 5

# 4. 从 Zotero 导入新论文（需要 MinerU）
python scripts/sync_zotero.py              # 自动定位 ../MinerU-GUI
python scripts/sync_zotero.py --dry-run    # 试运行
python scripts/sync_zotero.py --full-rescan  # 全量重建
```

详细说明见 [paper-knowledge-base/README.md](paper-knowledge-base/README.md)。
