# MinerU GUI

MinerU 文档解析桌面 GUI + Python API — 基于 CustomTkinter。

本目录是 [paper-knowledge-base monorepo](https://github.com/wolfmanster/paper-knowledge-base) 的子项目，也可独立使用。

## 快速开始

```bash
# 注册包（monorepo 中首次使用）
cd MinerU-GUI
python -m pip install -e .

# 安装 MinerU 引擎（核心依赖，必须用 3.x）
pip install "mineru[core]>=3.0.0,<4.0.0"
```

> **注意**: MinerU 是核心解析引擎，安装时需联网下载模型文件。
> 必须使用 MinerU **3.x** 版本（`<4.0.0`），4.x 尚未发布。

## 依赖

| 包 | 用途 |
|---|---|
| `customtkinter` | GUI 框架 |
| `windnd` | 文件拖拽支持 |
| `loguru` | 日志输出 |
| `mineru` | 文档解析引擎（**必须用 3.x**，需额外安装） |

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
pip install -e /path/to/MinerU/GUI
#       ↑ "-e" = editable，代码修改后立即生效

# 方式二：sys.path 临时添加
import sys
sys.path.insert(0, r"/path/to/MinerU/GUI")

# 无论哪种方式，都需确保 MinerU 引擎已安装
pip install "mineru[core]>=3.0.0,<4.0.0"
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
├── setup.bat               # 旧版安装脚本
└── start.bat               # 旧版启动脚本
```

## 许可

MIT
