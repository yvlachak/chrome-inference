from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120.0


@dataclass
class TaskState:
    task_id: str
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    created_at: float = field(default_factory=time.time)


class Broker:
    def __init__(self) -> None:
        self.jobs: queue.Queue[dict[str, Any]] = queue.Queue()
        self.tasks: dict[str, TaskState] = {}
        self.lock = threading.Lock()
        self.worker_last_seen = 0.0
        self.worker_capabilities: dict[str, Any] = {}

    def submit(self, op: str, payload: dict[str, Any], stream: bool = False) -> TaskState:
        task_id = str(uuid.uuid4())
        state = TaskState(task_id=task_id)
        with self.lock:
            self.tasks[task_id] = state
        self.jobs.put({"id": task_id, "op": op, "payload": payload, "stream": stream})
        return state

    def next_job(self, timeout: float = 25.0) -> dict[str, Any] | None:
        self.worker_last_seen = time.time()
        try:
            return self.jobs.get(timeout=timeout)
        except queue.Empty:
            return None

    def publish(self, event: dict[str, Any]) -> bool:
        task_id = event.get("id")
        if not isinstance(task_id, str):
            return False
        with self.lock:
            state = self.tasks.get(task_id)
        if state is None:
            return False
        state.events.put(event)
        return True

    def finish(self, task_id: str) -> None:
        with self.lock:
            self.tasks.pop(task_id, None)

    def heartbeat(self, capabilities: dict[str, Any] | None = None) -> None:
        self.worker_last_seen = time.time()
        if capabilities:
            self.worker_capabilities = capabilities

    def worker_connected(self) -> bool:
        return (time.time() - self.worker_last_seen) < 35.0


class ChromeInferenceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, worker_secret: str):
        super().__init__(address, RequestHandler)
        self.broker = Broker()
        self.api_token = token
        self.worker_secret = worker_secret


class RequestHandler(BaseHTTPRequestHandler):
    server: ChromeInferenceServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_OPTIONS(self) -> None:  # Deliberately no CORS surface.
        self.send_error(HTTPStatus.FORBIDDEN)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/worker", "/worker.html"}:
            self._serve_worker()
            return
        if path == "/worker.js":
            self._serve_static(WEB_DIR / "worker.js", "text/javascript; charset=utf-8")
            return
        if path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "worker_connected": self.server.broker.worker_connected(),
                    "worker_last_seen": self.server.broker.worker_last_seen or None,
                    "capabilities": self.server.broker.worker_capabilities,
                },
            )
            return
        if path == "/v1/capabilities":
            if not self._require_api_token():
                return
            self._dispatch("capabilities", {}, stream=False)
            return
        if path == "/internal/next":
            if not self._require_worker_cookie():
                return
            job = self.server.broker.next_job()
            self._json(HTTPStatus.OK, {"job": job})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/internal/event":
            if not self._require_worker_cookie():
                return
            event = self._read_json()
            if event is None:
                return
            accepted = self.server.broker.publish(event)
            self._json(HTTPStatus.OK, {"accepted": accepted})
            return
        if path == "/internal/heartbeat":
            if not self._require_worker_cookie():
                return
            body = self._read_json() or {}
            self.server.broker.heartbeat(body.get("capabilities"))
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if not self._require_api_token():
            return
        body = self._read_json()
        if body is None:
            return

        if path == "/v1/prompt":
            if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "prompt is required"})
                return
            self._dispatch("prompt", body, stream=False)
            return
        if path == "/v1/prompt/stream":
            if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "prompt is required"})
                return
            self._dispatch("prompt", body, stream=True)
            return
        if path == "/v1/classify":
            labels = body.get("labels")
            if not isinstance(body.get("input"), str) or not isinstance(labels, list) or len(labels) < 2:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "input and at least two labels are required"},
                )
                return
            self._dispatch("classify", body, stream=False)
            return
        if path == "/v1/summarize":
            if not isinstance(body.get("text"), str) or not body["text"].strip():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "text is required"})
                return
            self._dispatch("summarize", body, stream=False)
            return
        if path == "/v1/measure":
            if not isinstance(body.get("input"), str):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "input is required"})
                return
            self._dispatch("measure", body, stream=False)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _dispatch(self, op: str, payload: dict[str, Any], stream: bool) -> None:
        if not self.server.broker.worker_connected():
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "Chrome worker is not connected",
                    "hint": f"Open http://{self.server.server_address[0]}:{self.server.server_address[1]}/worker in Chrome",
                },
            )
            return

        state = self.server.broker.submit(op, payload, stream=stream)
        if stream:
            self._stream_events(state)
        else:
            self._wait_for_result(state)

    def _wait_for_result(self, state: TaskState) -> None:
        try:
            while True:
                event = state.events.get(timeout=DEFAULT_TIMEOUT)
                kind = event.get("kind")
                if kind == "result":
                    self._json(HTTPStatus.OK, event.get("result", {}))
                    return
                if kind == "error":
                    self._json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": event.get("error", "Chrome inference failed")},
                    )
                    return
        except queue.Empty:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "inference timed out"})
        finally:
            self.server.broker.finish(state.task_id)

    def _stream_events(self, state: TaskState) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = state.events.get(timeout=DEFAULT_TIMEOUT)
                except queue.Empty:
                    self._write_sse("error", {"error": "inference timed out"})
                    break
                kind = event.get("kind", "event")
                if kind == "chunk":
                    self._write_sse("chunk", {"text": event.get("text", "")})
                    continue
                if kind == "done":
                    self._write_sse("done", event.get("result", {}))
                    break
                if kind == "error":
                    self._write_sse("error", {"error": event.get("error", "Chrome inference failed")})
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.broker.finish(state.task_id)

    def _write_sse(self, event_name: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _serve_worker(self) -> None:
        path = WEB_DIR / "worker.html"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "worker.html missing")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Set-Cookie",
            f"ci_worker={self.server.worker_secret}; HttpOnly; SameSite=Strict; Path=/internal",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {exc}"})
            return None

    def _require_api_token(self) -> bool:
        expected = f"Bearer {self.server.api_token}"
        if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid bearer token"})
            return False
        return True

    def _require_worker_cookie(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        expected = f"ci_worker={self.server.worker_secret}"
        if expected not in cookies:
            self._json(HTTPStatus.FORBIDDEN, {"error": "worker authentication failed"})
            return False
        return True

    def _json(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def config_dir() -> Path:
    configured = os.environ.get("CHROME_INFERENCE_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".chrome-inference"


def load_or_create_token() -> tuple[str, Path]:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            token = config.get("token")
            if isinstance(token, str) and token:
                return token, path
        except (OSError, json.JSONDecodeError):
            pass
    token = secrets.token_urlsafe(32)
    path.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
    return token, path


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose Chrome Built-in AI on localhost")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Chrome Inference binds to loopback only; use 127.0.0.1, localhost, or ::1")

    token, config_path = load_or_create_token()
    worker_secret = secrets.token_urlsafe(32)
    server = ChromeInferenceServer((args.host, args.port), token, worker_secret)

    print(f"Chrome Inference broker: http://{args.host}:{args.port}")
    print(f"Worker page:             http://{args.host}:{args.port}/worker")
    print(f"Client config:           {config_path}")
    print("Keep the worker page open in Chrome while using the localhost API.")

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Chrome Inference broker.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
