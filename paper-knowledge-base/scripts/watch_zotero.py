"""Watch Zotero for changes and run the MinerU knowledge-base sync.

The watcher only uses the Python standard library. It can therefore run on
Windows, macOS, or Linux after the normal project dependencies are installed.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = Path(__file__).resolve().parent / "sync_zotero.py"
DEFAULT_ZOTERO_DIR = Path.home() / "Zotero"
DEFAULT_LOG_FILE = REPO_ROOT / "kb" / "watch_zotero.log"
DEFAULT_INTERVAL_SECONDS = 86_400.0

DatabaseSignature = tuple[tuple[str, int, int], ...]


def database_signature(database: Path) -> DatabaseSignature | None:
    """Return a signature covering SQLite itself and its live WAL file."""
    paths = (database, Path(f"{database}-wal"))
    parts: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            logging.getLogger(__name__).debug(
                "无法读取 Zotero 数据库状态: %s", path, exc_info=True
            )
            continue
        parts.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(parts) or None


def build_sync_command(
    *,
    python_executable: str,
    sync_script: Path,
    zotero_dir: Path,
    mineru_dir: Path | None,
) -> list[str]:
    """Build a portable subprocess command for one incremental sync."""
    command = [
        python_executable,
        str(sync_script),
        "--zotero-dir",
        str(zotero_dir),
    ]
    if mineru_dir is not None:
        command.extend(["--mineru-dir", str(mineru_dir)])
    return command


def run_sync(zotero_dir: Path, mineru_dir: Path | None) -> bool:
    """Run one isolated sync and return whether it completed successfully."""
    command = build_sync_command(
        python_executable=sys.executable,
        sync_script=SYNC_SCRIPT,
        zotero_dir=zotero_dir,
        mineru_dir=mineru_dir,
    )
    logging.getLogger(__name__).info("开始 Zotero 增量同步")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode == 0:
        logging.getLogger(__name__).info("Zotero 增量同步完成")
        return True
    logging.getLogger(__name__).error(
        "Zotero 增量同步失败，退出码: %d；稍后将自动重试",
        completed.returncode,
    )
    return False


def wait_until_stable(
    database: Path,
    signature: DatabaseSignature,
    settle_seconds: float,
) -> DatabaseSignature:
    """Wait until Zotero has stopped writing the database for one settle period."""
    current = signature
    while settle_seconds > 0:
        time.sleep(settle_seconds)
        updated = database_signature(database)
        if updated is None or updated == current:
            return current
        current = updated
    return current


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, mode="a", encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.insert(0, logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="监听 Zotero 变化，自动通过 MinerU 同步到论文向量库"
    )
    parser.add_argument(
        "--zotero-dir",
        type=Path,
        default=DEFAULT_ZOTERO_DIR,
        help="Zotero 数据目录（默认: ~/Zotero）",
    )
    parser.add_argument(
        "--mineru-dir",
        type=Path,
        default=None,
        help="MinerU-GUI 目录（默认自动查找 monorepo 中的兄弟目录）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="数据库检查间隔秒数（默认: 86400，即每天一次）",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=5.0,
        help="检测到变化后等待 Zotero 写入稳定的秒数（默认: 5）",
    )
    parser.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="启动时不立即执行一次增量同步",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help="监听日志路径",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    if args.settle_seconds < 0:
        parser.error("--settle-seconds 不能小于 0")

    zotero_dir = args.zotero_dir.expanduser().resolve()
    mineru_dir = args.mineru_dir.expanduser().resolve() if args.mineru_dir else None
    database = zotero_dir / "zotero.sqlite"
    configure_logging(args.log_file.expanduser().resolve())
    logger = logging.getLogger(__name__)

    logger.info("Zotero 自动同步监听器启动")
    logger.info("  Zotero 数据库: %s", database)
    logger.info("  检查间隔: %.1f 秒", args.interval)

    last_synced_signature = database_signature(database)
    if last_synced_signature is None:
        logger.warning("Zotero 数据库暂不存在，将持续等待: %s", database)
    elif not args.no_initial_sync:
        stable = wait_until_stable(
            database, last_synced_signature, args.settle_seconds
        )
        if run_sync(zotero_dir, mineru_dir):
            last_synced_signature = stable
        else:
            last_synced_signature = None

    try:
        while True:
            time.sleep(args.interval)
            current = database_signature(database)
            if current is None:
                if last_synced_signature is not None:
                    logger.warning("Zotero 数据库暂不可用，将继续等待")
                last_synced_signature = None
                continue
            if current == last_synced_signature:
                continue

            logger.info("检测到 Zotero 数据变化，等待写入稳定")
            stable = wait_until_stable(database, current, args.settle_seconds)
            if run_sync(zotero_dir, mineru_dir):
                # Keep the pre-sync signature. If Zotero changes during a long
                # MinerU run, the next poll will observe it and sync again.
                last_synced_signature = stable
    except KeyboardInterrupt:
        logger.info("Zotero 自动同步监听器已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
