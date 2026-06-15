#!/usr/bin/env python3
"""Minimal OpenAI-compatible upstream that records the max_tokens it receives.

Its only job is to prove what value LiteLLM actually sends downstream after the
get_modified_max_tokens rewrite. It prints the received max_tokens to stdout and
echoes it back in the assistant message so the caller sees it too.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        received = body.get("max_tokens")
        print(f"MOCK_UPSTREAM received max_tokens={received}", flush=True)

        resp = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "bug-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": f"upstream_received_max_tokens={received}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print("MOCK_UPSTREAM listening on :9000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
