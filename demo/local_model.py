"""Free local model adapter: an open-weights LLM served by Ollama on this machine.

No API key, no cost. Default model qwen3:8b (`AGENTWAL_OLLAMA_MODEL` to change);
server at http://127.0.0.1:11434 (`OLLAMA_HOST` to change). It samples like any real
model, so its decisions are not reproducible run to run, which is exactly what the
journal has to cope with.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

DEFAULT_MODEL = "qwen3:8b"


class OllamaModel:
    def __init__(self, model: str | None = None, billing_log: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("AGENTWAL_OLLAMA_MODEL", DEFAULT_MODEL)
        self.name = self.model
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.billing_log = billing_log

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": request["system"]}, *request["messages"]],
            "stream": False,
            "think": False,  # qwen3: answer directly, no reasoning trace
            "format": "json",
            "options": {"num_predict": 128},
        }
        req = urllib.request.Request(self.host + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read())
        text = resp["message"]["content"]
        usage = {"input_tokens": resp.get("prompt_eval_count", 0), "output_tokens": resp.get("eval_count", 0)}
        if self.billing_log:
            with open(self.billing_log, "a") as f:
                f.write(json.dumps({"cost_usd": 0.0, "model": self.model, **usage}) + "\n")
        return {"text": text, "usage": usage, "model": self.model}
