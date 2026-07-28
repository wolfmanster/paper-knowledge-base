# MinerU Python API 参考文档

> 本项目的文档转换核心封装在 `mineru_api.py`，不依赖 GUI（CustomTkinter/tkinter），
> 可在其他 Python 项目中直接调用，特别适合 AI Agent 项目自动化解析文档。

---

## 安装

```python
# ── 方式 A：pip 安装（推荐，无需 sys.path.insert）──
# 在 MinerU GUI 项目目录下执行：
#   pip install -e .
# 之后任何地方都可以：
from mineru_api import convert_document, batch_convert, ConversionResult

# ── 方式 B：sys.path 临时添加（适合不想安装的场景）──
import sys
sys.path.insert(0, r"/path/to/MinerU GUI")
from mineru_api import convert_document, batch_convert, ConversionResult
```

> **前提依赖**：MinerU 引擎需单独安装：
> ```bash
> pip install mineru[core]
> ```

---

## `convert_document()` — 单个文件转换

```python
def convert_document(
    file_path: str | Path,
    backend: str = "hybrid-auto-engine",
    lang: str = "ch",
    method: str = "auto",
    max_pages: int = 0,
    device: str = "gpu",
    vlm_describe: bool = False,
    output_dir: str | Path | None = None,
) -> ConversionResult:
    ...
```

### 参数说明

| 参数 | 类型 | 默认值 | 可选值 | 说明 |
|------|------|--------|--------|------|
| `file_path` | `str\|Path` | — | 任何支持的文档路径 | 输入文件。支持 PDF / PNG / JPG / JPEG / JP2 / WebP / GIF / BMP / TIFF / DOCX |
| `backend` | `str` | `"hybrid-auto-engine"` | `"pipeline"` / `"hybrid-auto-engine"` | 解析后端。hybrid 引擎更准确但需要 GPU |
| `lang` | `str` | `"ch"` | 见下方语言表 | OCR 识别语言 |
| `method` | `str` | `"auto"` | `"auto"` / `"txt"` / `"ocr"` | 解析方法 |
| `max_pages` | `int` | `0` | `0`–`1000` | 最大处理页数。`0` = 全部 |
| `device` | `str` | `"gpu"` | `"cpu"` / `"gpu"` | 运行设备。`hybrid-auto-engine` 后端固定使用 GPU |
| `vlm_describe` | `bool` | `False` | `True` / `False` | 是否用 VLM 模型描述图片。**仅 hybrid-auto-engine 有效** |
| `output_dir` | `str\|Path\|None` | `None` | 任意路径或 `None` | 自定义输出目录。`None` = 自动在 `./output/<文件名>/` 下生成 |

### 返回值 `ConversionResult`

```python
@dataclass
class ConversionResult:
    success: bool = False         # 是否成功
    file_path: str = ""           # 输入文件路径
    output_md: Path | None = None # 生成的 Markdown 文件路径（成功时）
    output_dir: Path | None = None# 输出目录（成功时）
    log_lines: list[str] = []     # 处理过程中的所有日志行
    error: str | None = None      # 错误信息（失败时）
```

### 基本示例

```python
from mineru_api import convert_document

# 最简调用（混合引擎 + GPU + 中文）
result = convert_document("report.pdf")
if result.success:
    print(f"✅ 成功，Markdown 文件：{result.output_md}")
else:
    print(f"❌ 失败：{result.error}")
```

### 自定义输出目录

```python
result = convert_document(
    "invoice.png",
    output_dir="./parsed_output/invoice",
    device="cpu",    # 无 GPU 时使用 CPU
)
```

---

## `batch_convert()` — 批量文件转换

```python
def batch_convert(
    file_paths: list[str | Path],
    backend: str = "hybrid-auto-engine",
    lang: str = "ch",
    method: str = "auto",
    max_pages: int = 0,
    device: str = "gpu",
    vlm_describe: bool = False,
    output_dir: str | Path | None = None,
) -> list[ConversionResult]:
    ...
```

参数与 `convert_document` 一致，但 `file_paths` 接受文件路径列表。返回结果顺序与输入一致。

### 批量示例

```python
from mineru_api import batch_convert

files = ["doc1.pdf", "doc2.pdf", "image.png"]
results = batch_convert(files, device="gpu")

# 统计
ok = [r for r in results if r.success]
fail = [r for r in results if not r.success]
print(f"共 {len(files)} 个，成功 {len(ok)}，失败 {len(fail)}")

# 逐项输出
for r in results:
    status = "✅" if r.success else "❌"
    md = r.output_md or r.error
    print(f"{status} {r.file_path} → {md}")
```

---

## 注意事项（AI 项目调用时容易忽略）

### ⚠️ 线程不安全

`convert_document` 内部设置了进程级环境变量（`MINERU_DEVICE_MODE`、`OMP_NUM_THREADS` 等），
**不是线程安全的**。不要在多个线程中同时调用。

```python
# ❌ 错误：多个线程同时调用
import threading
t1 = threading.Thread(target=convert_document, args=("a.pdf",))
t2 = threading.Thread(target=convert_document, args=("b.pdf",))
t1.start(); t2.start()  # 环境变量竞争，结果不可预测

# ✅ 正确：用 multiprocessing 并发
from multiprocessing import Process

def run_convert(path):
    result = convert_document(path)
    print(result.output_md)

Process(target=run_convert, args=("a.pdf",)).start()
Process(target=run_convert, args=("b.pdf",)).start()
```

### ⚠️ VLM 图片描述

`vlm_describe=True` **仅在 `hybrid-auto-engine` 后端有效**。它依赖 `do_parse()` 加载的 VLM 模型
（`ModelSingleton` 进程级单例），在当前进程中只能有一份。如果用 `pipeline` 后端，此参数无效果。

### ⚠️ 输出目录

默认输出路径为 `./output/<文件名>/`。如果同一文件转换多次，后续结果会覆盖之前的 `.md` 文件。

如果想避免覆盖，每次传入不同的 `output_dir`：
```python
import time
result = convert_document("report.pdf", output_dir=f"./output/report_{int(time.time())}")
```

### ⚠️ 环境变量污染

`setup_env()` 会修改 `os.environ`。如果在同一进程中先后用不同 `device` 调用，
后一次的环境变量会影响前一次仍在运行的任务。建议每次调用前在子进程中进行。

---

## 语言代码（OCR）

| 代码 | 语言 |
|------|------|
| `ch` | 中文 |
| `en` | 英文 |
| `korean` | 韩语 |
| `japan` | 日语 |
| `chinese_cht` | 繁体中文 |
| `ta` | 泰米尔语 |
| `te` | 泰卢固语 |
| `ka` | 格鲁吉亚语 |
| `th` | 泰语 |
| `el` | 希腊语 |
| `latin` | 拉丁语系 |
| `arabic` | 阿拉伯语 |
| `east_slavic` | 东斯拉夫语 |
| `cyrillic` | 西里尔语 |
| `devanagari` | 梵文 |

完整列表见 [`app.py`](app.py) 的 `LANGUAGES` 变量。

---

## 支持的文件类型

PDF、PNG、JPG、JPEG、JP2、WebP、GIF、BMP、TIFF、DOCX

```python
# 判断文件是否被支持：
from pathlib import Path
from app import SUPPORTED_EXTENSIONS

def is_supported(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
```

---

## 完整可运行示例

保存以下内容为 `demo.py`，放在项目根目录即可运行：

```python
"""MinerU conversion demo — 单文件和批量转换示例。"""
import sys
from pathlib import Path

# 如果未通过 pip install -e . 安装，取消下面注释：
# sys.path.insert(0, r"/path/to/MinerU GUI")

from mineru_api import convert_document, batch_convert


def demo_single():
    """单文件转换示例。"""
    pdf_path = "demo.pdf"
    if not Path(pdf_path).exists():
        print(f"⚠ 文件 {pdf_path} 不存在，跳过单文件示例")
        return

    print("=" * 60)
    print("📄 单文件转换")
    print("=" * 60)

    result = convert_document(
        pdf_path,
        backend="pipeline",   # CPU 友好的后端
        device="cpu",
        lang="ch",
        output_dir="./output/demo_single",
    )
    if result.success:
        print(f"✅ 成功")
        print(f"   输出 Markdown：{result.output_md}")
        print(f"   输出目录：{result.output_dir}")
    else:
        print(f"❌ 失败：{result.error}")


def demo_batch():
    """批量转换示例。"""
    sources = list(Path(".").glob("*.pdf")) + list(Path(".").glob("*.png"))
    if not sources:
        print("⚠ 当前目录没有 PDF 或 PNG 文件，跳过批量示例")
        return

    print("=" * 60)
    print(f"📚 批量转换（共 {len(sources)} 个文件）")
    print("=" * 60)

    results = batch_convert(
        [str(p) for p in sources],
        device="cpu",
        output_dir="./output/batch_demo",
    )

    succ = fail = 0
    for r in results:
        if r.success:
            succ += 1
            print(f"  ✅ {Path(r.file_path).name} → {r.output_md}")
        else:
            fail += 1
            print(f"  ❌ {Path(r.file_path).name} → {r.error}")

    print(f"\n📊 汇总：成功 {succ} / 失败 {fail}")


if __name__ == "__main__":
    demo_single()
    print()
    demo_batch()
```
