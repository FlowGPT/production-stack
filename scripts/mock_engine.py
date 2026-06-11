#!/usr/bin/env python3
"""Minimal mock vLLM engine for router fallback testing.

Exposes just what the router needs:
  GET  /health             -> 200
  GET  /metrics            -> prometheus text with a CONFIGURABLE
                              vllm:num_requests_waiting (to simulate overload)
  POST /v1/completions     -> instant dummy completion

Usage: mock_engine.py <port> <queue_waiting>
A high <queue_waiting> (>= router --cache-aware-tolerate-waiting-requests) makes
this engine look overloaded, so sessions hashing to it fall back to another.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
QUEUE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence per-request logging
        pass

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok")
        elif self.path == "/metrics":
            body = (
                "# TYPE vllm:num_requests_running gauge\n"
                "vllm:num_requests_running 0.0\n"
                "# TYPE vllm:num_requests_waiting gauge\n"
                f"vllm:num_requests_waiting {QUEUE}\n"
            ).encode()
            self._send(200, body)
        else:
            self._send(404, b"")

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        if length:
            self.rfile.read(length)
        body = json.dumps(
            {
                "id": "cmpl-mock",
                "object": "text_completion",
                "model": "mock-model",
                "choices": [
                    {"index": 0, "text": "hi", "finish_reason": "length"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self._send(200, body, "application/json")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
