"""零依赖 Mock OpenAI 兼容 LLM 服务（仅标准库，支持流式 SSE / 非流式）。

用途：并发压测时替代真实上游，零配额消耗且延迟可控。

环境变量：
- MOCK_LLM_DELAY_MS：首 token 前延迟（默认 300ms）
- MOCK_LLM_CHUNKS：流式 chunk 数（默认 16）
- MOCK_LLM_CHUNK_INTERVAL_MS：chunk 间隔（默认 60ms）
- MOCK_LLM_PORT：监听端口（默认 8000）

运行：python tests/load/mock_llm_server.py
"""

from __future__ import annotations

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY_MS = int(os.environ.get("MOCK_LLM_DELAY_MS", "300"))
CHUNKS = int(os.environ.get("MOCK_LLM_CHUNKS", "16"))
INTERVAL_MS = int(os.environ.get("MOCK_LLM_CHUNK_INTERVAL_MS", "60"))
PORT = int(os.environ.get("MOCK_LLM_PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静默访问日志
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.endswith("/chat/completions"):
            self._send_json(404, {"error": "not found"})
            return
        payload = self._read_json()
        stream = bool(payload.get("stream"))
        model = payload.get("model", "mock-model")
        created = int(time.time())

        if stream:
            cid = f"chatcmpl-{uuid.uuid4().hex}"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            time.sleep(DELAY_MS / 1000)
            for i in range(CHUNKS):
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": f"模拟{i} "}, "finish_reason": None}
                    ],
                }
                self._send_chunk(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                time.sleep(INTERVAL_MS / 1000)
            final = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self._send_chunk(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode())
            usage = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": CHUNKS,
                    "total_tokens": 100 + CHUNKS,
                },
            }
            self._send_chunk(f"data: {json.dumps(usage, ensure_ascii=False)}\n\n".encode())
            self._send_chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        time.sleep((DELAY_MS + CHUNKS * INTERVAL_MS) / 1000)
        self._send_json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "这是模拟回复。"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"mock-llm listening on :{PORT} delay={DELAY_MS}ms chunks={CHUNKS} interval={INTERVAL_MS}ms")
    server.serve_forever()


if __name__ == "__main__":
    main()