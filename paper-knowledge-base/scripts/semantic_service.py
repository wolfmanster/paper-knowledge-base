"""常驻语义检索服务。

默认只监听 127.0.0.1。服务进程一次性加载两个模型，query.py 的后续调用通过
本机 HTTP 复用模型和 ChromaDB 连接。
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from index_generation import read_index_generation
from utils import (
    BASE_DIR,
    load_bi_encoder,
    load_cross_encoder,
    get_or_create_chroma_collection,
    ensure_utf8_stdout,
)

HOST = os.environ.get("PKB_SEMANTIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("PKB_SEMANTIC_PORT", "8765"))
STARTUP_TIMEOUT = float(os.environ.get("PKB_SEMANTIC_START_TIMEOUT", "90"))
REQUEST_TIMEOUT = float(os.environ.get("PKB_SEMANTIC_REQUEST_TIMEOUT", "120"))
MAX_REQUEST_BYTES = 64 * 1024
SERVICE_NAME = "paper-knowledge-base-semantic"
PROTOCOL_VERSION = 1
LOG_PATH = BASE_DIR / "kb" / "semantic_service.log"


class SemanticServiceError(RuntimeError):
    """常驻服务无法启动或无法完成请求。"""


class SemanticServiceConnectionError(SemanticServiceError):
    """目标端口没有进程监听。"""


class SemanticIndexChangedError(SemanticServiceError):
    """磁盘上的 Chroma 索引已由其他进程更新。"""


class SemanticRuntime:
    """持有只加载一次的模型、Chroma 集合及串行搜索锁。"""

    def __init__(
        self,
        module_loader: Callable[[], Any] | None = None,
        generation_reader: Callable[[], str] | None = None,
    ):
        self._module_loader = module_loader or self._load_query_module
        self._generation_reader = generation_reader or read_index_generation
        self._state_lock = threading.Lock()
        self._search_lock = threading.Lock()
        self._status = "loading"
        self._error = ""
        self._query_module = None
        self._bi_encoder = None
        self._cross_encoder = None
        self._collection = None
        self._device = "unknown"
        self._index_generation = "0"

    @staticmethod
    def _load_query_module():
        import query

        return query

    def load(self) -> None:
        try:
            query_module = self._module_loader()
            bi_encoder = load_bi_encoder()
            cross_encoder = load_cross_encoder()
            generation_before = self._generation_reader()
            collection = get_or_create_chroma_collection()
            generation_after = self._generation_reader()
            if generation_before != generation_after:
                raise SemanticIndexChangedError("模型预热期间索引发生变化，请重试")
        except SystemExit as exc:
            with self._state_lock:
                self._error = f"初始化失败（退出码 {exc.code}），详见服务日志"
                self._status = "error"
            return
        except Exception as exc:
            with self._state_lock:
                self._error = str(exc) or type(exc).__name__
                self._status = "error"
            return

        with self._state_lock:
            self._query_module = query_module
            self._bi_encoder = bi_encoder
            self._cross_encoder = cross_encoder
            self._collection = collection
            self._device = str(getattr(bi_encoder, "device", "unknown"))
            self._index_generation = generation_after
            self._status = "ready"

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            health = {
                "service": SERVICE_NAME,
                "protocol": PROTOCOL_VERSION,
                "status": self._status,
                "pid": os.getpid(),
                "device": self._device,
                "generation": self._index_generation,
                "error": self._error,
            }
        if health["status"] == "ready":
            try:
                current_generation = self._generation_reader()
            except OSError as exc:
                health["status"] = "error"
                health["error"] = f"无法读取索引代际: {exc}"
            else:
                if current_generation != health["generation"]:
                    health["status"] = "stale"
        return health

    def search(self, query_text: str, top_k: int) -> list[dict]:
        with self._state_lock:
            if self._status != "ready":
                detail = self._error or "模型仍在预热"
                raise SemanticServiceError(detail)
            query_module = self._query_module
            bi_encoder = self._bi_encoder
            cross_encoder = self._cross_encoder
            collection = self._collection

        with self._search_lock:
            if self._generation_reader() != self._index_generation:
                raise SemanticIndexChangedError("索引已更新，正在重启语义服务")
            results = query_module.search_with_components(
                query=query_text,
                top_k=top_k,
                bi_encoder=bi_encoder,
                cross_encoder=cross_encoder,
                collection=collection,
            )
            if self._generation_reader() != self._index_generation:
                raise SemanticIndexChangedError("搜索期间索引发生更新，正在重试")
            return results


class SemanticHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, runtime: SemanticRuntime):
        self.runtime = runtime
        super().__init__(server_address, SemanticRequestHandler)


class SemanticRequestHandler(BaseHTTPRequestHandler):
    server: SemanticHTTPServer

    def log_message(self, format: str, *args) -> None:
        if self.path == "/health":
            return
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("请求体为空或过大")
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, self.server.runtime.health())

    def do_POST(self) -> None:
        if self.path == "/shutdown":
            self._send_json(200, {"status": "stopping", "pid": os.getpid()})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != "/search":
            self._send_json(404, {"error": "not found"})
            return

        try:
            payload = self._read_json()
            query_text = payload.get("query")
            top_k = payload.get("top_k", 5)
            if not isinstance(query_text, str) or not query_text.strip():
                raise ValueError("query 必须是非空字符串")
            if isinstance(top_k, bool) or not isinstance(top_k, int):
                raise ValueError("top_k 必须是整数")
            if not 1 <= top_k <= 100:
                raise ValueError("top_k 必须在 1 到 100 之间")
            results = self.server.runtime.search(query_text.strip(), top_k)
        except SemanticIndexChangedError as exc:
            self._send_json(409, {"error": str(exc), "code": "index_changed"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SemanticServiceError as exc:
            self._send_json(503, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(500, {"error": f"语义检索失败: {exc}"})
            return

        self._send_json(200, results)


def create_server(
    host: str = HOST,
    port: int = PORT,
    runtime: SemanticRuntime | None = None,
) -> SemanticHTTPServer:
    return SemanticHTTPServer((host, port), runtime or SemanticRuntime())


def _configure_utf8_streams() -> None:
    ensure_utf8_stdout()


def _load_runtime_and_stop_on_error(
    runtime: SemanticRuntime,
    server: SemanticHTTPServer,
) -> None:
    runtime.load()
    if runtime.health()["status"] == "error":
        server.shutdown()


def run_server(host: str = HOST, port: int = PORT) -> None:
    _configure_utf8_streams()
    runtime = SemanticRuntime()
    server = create_server(host, port, runtime)
    loader = threading.Thread(
        target=_load_runtime_and_stop_on_error,
        args=(runtime, server),
        name="semantic-model-loader",
        daemon=True,
    )
    loader.start()
    print(f"语义检索服务已监听 http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def _service_url(path: str, host: str = HOST, port: int = PORT) -> str:
    return f"http://{host}:{port}{path}"


def _request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.0,
    host: str = HOST,
    port: int = PORT,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        _service_url(path, host, port), data=body, headers=headers, method=method
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = error_payload.get("error", str(exc))
            error_code = error_payload.get("code")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = str(exc)
            error_code = None
        if error_code == "index_changed":
            raise SemanticIndexChangedError(detail) from exc
        raise SemanticServiceError(detail) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if (
            isinstance(
                reason,
                (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, TimeoutError),
            )
            or getattr(reason, "errno", None) == errno.ECONNREFUSED
            or getattr(reason, "winerror", None) == 10061
        ):
            raise SemanticServiceConnectionError(str(exc)) from exc
        raise SemanticServiceError(str(exc)) from exc
    except (ConnectionResetError, ConnectionAbortedError, TimeoutError) as exc:
        raise SemanticServiceConnectionError(str(exc)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticServiceError(str(exc)) from exc


def get_health(
    timeout: float = 0.5,
    host: str = HOST,
    port: int = PORT,
) -> dict[str, Any]:
    health = _request_json("GET", "/health", timeout=timeout, host=host, port=port)
    if not isinstance(health, dict) or health.get("service") != SERVICE_NAME:
        raise SemanticServiceError("端口上的进程不是论文知识库语义检索服务")
    if health.get("protocol") != PROTOCOL_VERSION:
        raise SemanticServiceError("语义检索服务协议版本不兼容，请重启服务")
    return health


def start_background(host: str = HOST, port: int = PORT) -> int:
    """启动脱离当前终端的后台服务，返回子进程 PID。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "serve"]
    environment = os.environ.copy()
    environment["PKB_SEMANTIC_HOST"] = host
    environment["PKB_SEMANTIC_PORT"] = str(port)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "env": environment}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    with LOG_PATH.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=log_file,
            **kwargs,
        )
    return process.pid


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_service_stop(
    host: str,
    port: int,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            get_health(timeout=0.2, host=host, port=port)
        except SemanticServiceConnectionError:
            return
        except SemanticServiceError:
            pass
        time.sleep(0.1)
    raise SemanticServiceError("旧语义服务未能及时退出")


def ensure_ready(
    timeout: float = STARTUP_TIMEOUT,
    auto_start: bool = True,
    host: str = HOST,
    port: int = PORT,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    started_pid: int | None = None
    last_error = "服务无响应"

    while time.monotonic() < deadline:
        try:
            health = get_health(host=host, port=port)
        except SemanticServiceConnectionError as exc:
            last_error = str(exc)
            if auto_start and started_pid is None:
                started_pid = start_background(host=host, port=port)
            elif started_pid is not None and not _process_is_running(started_pid):
                raise SemanticServiceError(
                    f"后台语义服务进程提前退出（PID {started_pid}）；日志: {LOG_PATH}"
                ) from exc
        except SemanticServiceError:
            # 端口被其他 HTTP 服务占用、协议不兼容或请求超时都不能通过
            # 再启动一个同端口进程解决，应立即向调用者报告。
            raise
        else:
            status = health.get("status")
            if status == "ready":
                return health
            if status == "error":
                raise SemanticServiceError(health.get("error") or "模型预热失败")
            if status == "stale":
                _request_json(
                    "POST",
                    "/shutdown",
                    {},
                    timeout=2.0,
                    host=host,
                    port=port,
                )
                _wait_for_service_stop(host, port)
                started_pid = None
                continue
        time.sleep(0.2)

    raise SemanticServiceError(
        f"服务在 {timeout:g} 秒内未就绪: {last_error}；日志: {LOG_PATH}"
    )


def remote_search(
    query_text: str,
    top_k: int = 5,
    *,
    host: str = HOST,
    port: int = PORT,
    auto_start: bool = True,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> list[dict]:
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query 必须是非空字符串")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k 必须是 1 到 100 之间的整数")

    for attempt in range(2):
        ensure_ready(
            timeout=startup_timeout,
            auto_start=auto_start,
            host=host,
            port=port,
        )
        try:
            results = _request_json(
                "POST",
                "/search",
                {"query": query_text.strip(), "top_k": top_k},
                timeout=REQUEST_TIMEOUT,
                host=host,
                port=port,
            )
        except SemanticIndexChangedError:
            if attempt:
                raise
            _wait_for_service_stop(host, port)
            continue
        break
    else:  # pragma: no cover - 循环只会通过 return/break/raise 结束
        raise SemanticServiceError("语义搜索重试失败")
    if not isinstance(results, list):
        raise SemanticServiceError("服务返回了无效的搜索结果")
    return results


def stop_service(
    host: str = HOST,
    port: int = PORT,
) -> dict[str, Any]:
    get_health(host=host, port=port)
    result = _request_json(
        "POST",
        "/shutdown",
        {},
        timeout=2.0,
        host=host,
        port=port,
    )
    _wait_for_service_stop(host, port)
    return result


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    if command == "serve":
        run_server()
        return 0
    if command == "start":
        try:
            health = ensure_ready()
        except SemanticServiceError as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 0
    if command == "status":
        try:
            health = get_health()
        except SemanticServiceError as exc:
            print(json.dumps({"status": "stopped", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 0
    if command == "stop":
        try:
            result = stop_service()
        except SemanticServiceError as exc:
            print(json.dumps({"status": "stopped", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("用法: python semantic_service.py [start|serve|status|stop]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
