"""MinerU 文档解析工具 — CTk 桌面 GUI 启动入口。"""
import ctypes
import sys

# Enable high-DPI awareness before any GUI init
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

# Configure CTk appearance before importing MainWindow
from gui.theme import setup_ctk

setup_ctk()

from gui.main_window import MainWindow


def main():
    """启动 MinerU GUI 桌面应用。"""
    app = MainWindow()
    app.run()


if __name__ == "__main__":
    main()
