@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════╗
echo   ║          MinerU  🛠 Setup            ║
echo   ╚══════════════════════════════════════╝
echo.

REM ── Check Python ──────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3.10+。
    pause
    exit /b 1
)

REM ── Check if .venv already exists ─────────
if exist ".venv\Scripts\python.exe" (
    echo [INFO] 虚拟环境已存在，跳过创建。
    echo.
    choice /C YN /M "是否重新创建"
    if errorlevel 2 goto :install_deps
    echo 正在重新创建虚拟环境...
    rmdir /s /q .venv
)

echo 正在创建虚拟环境...
python -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] 虚拟环境创建失败。
    pause
    exit /b 1
)
echo 虚拟环境创建完成。

:install_deps
echo.
echo 正在安装 GUI 依赖...
.venv\Scripts\pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] 依赖安装失败。
    pause
    exit /b 1
)

echo.
echo   ────────────────────────────────────────
echo   ✓ Setup 完成
echo   ────────────────────────────────────────
echo.
echo   下一步:
echo     1. 安装 MinerU 引擎:
echo        .venv\Scripts\pip install mineru[core]
echo.
echo     2. 启动:
echo        start.bat
echo.
