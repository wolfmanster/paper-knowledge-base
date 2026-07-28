# MinerU GUI

MinerU 文档解析桌面 GUI — 基于 CustomTkinter。

## 快速开始

```bat
:: 1. 克隆仓库
git clone https://github.com/你的用户名/mineru-gui
cd mineru-gui

:: 2. 一键安装（虚拟环境 + GUI 依赖）
setup.bat

:: 3. 安装 MinerU 引擎（核心依赖）
.venv\Scripts\pip install mineru[core]

:: 4. 启动
start.bat
```

> **注意**: MinerU 是核心解析引擎，安装时需联网下载模型文件。
> 如果已有 MinerU 环境，可直接将本项目的 `gui.py` 放在你的环境中运行。

## 依赖

| 包 | 用途 |
|---|---|
| `customtkinter` | GUI 框架 |
| `windnd` | 文件拖拽支持 |
| `loguru` | 日志输出 |
| `mineru` | 文档解析引擎（需额外安装） |

## 使用

1. 拖拽或点击选择 PDF / 图片 / Office 文件
2. 选择解析参数（后端、语言、方法、设备、最大页数）
3. 点击"开始转换"
4. 输出文件在 `output/` 目录下

## Python API 调用

本项目的核心转换逻辑封装在 `mineru_api.py` 中，**不依赖 GUI，可在其他 Python 项目中直接调用**。

详见完整 API 参考文档：[API_REFERENCE.md](./API_REFERENCE.md)

### 安装

```bash
# 方式一：pip 安装（推荐，import 路径干净）
pip install -e /path/to/MinerU\ GUI
#       ↑ "-e" = editable，代码修改后立即生效

# 方式二：sys.path 临时添加
import sys
sys.path.insert(0, r"/path/to/MinerU GUI")

# 无论哪种方式，都需确保 MinerU 引擎已安装
pip install mineru[core]
```

### 使用示例

```python
from mineru_api import convert_document, batch_convert

# ── 单个文件 ──
result = convert_document("report.pdf")
if result.success:
    print(f"✓ 输出: {result.output_md}")

# ── 批量转换 ──
results = batch_convert(["a.pdf", "b.png", "c.docx"], device="cpu")
for r in results:
    status = "✓" if r.success else "✗"
    print(f"{status} {r.file_path} → {r.output_md or r.error}")

# ── 自定义输出目录 ──
result = convert_document("doc.pdf", output_dir="./my_output/doc")
```

## 项目结构

```
├── gui.py                  # 入口（GUI 模式）
├── mineru_api.py           # Python API（外部项目调用，无 GUI 依赖）
├── app.py                  # 常量 + 工具函数
├── gui/
│   ├── _core.py            # 共享转换核心（GUI 和 API 共用）
│   ├── theme.py            # 主题与字体
│   ├── main_window.py      # 主窗口
│   ├── worker.py           # 后台转换线程
│   └── widgets/            # UI 组件
├── output/                 # 解析结果输出
├── requirements.txt        # Python 依赖
├── setup.bat               # 安装脚本
└── start.bat               # 启动脚本
```

## 许可

MIT
