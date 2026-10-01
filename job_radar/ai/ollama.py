"""A minimal Ollama client: one prompt in, one JSON object out."""

import json
import urllib.error
import urllib.request
from typing import Any


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, url: str, model: str, timeout: float = 120):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise OllamaError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise OllamaError(str(e)) from e

    def check(self) -> None:
        """Raise OllamaError when the server is down or the model is not installed."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=5) as response:
                models = {m["name"] for m in json.loads(response.read()).get("models", [])}
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise OllamaError(f"Ollama is not reachable at {self.url}: {e}") from e
        if self.model not in models and f"{self.model}:latest" not in models:
            raise OllamaError(f"model {self.model} is not installed; run: ollama pull {self.model}")

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "prompt": prompt,
            "format": schema,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 600},
        }
        # Small models now and then stop inside a string; one retry is usually enough.
        for attempt in range(2):
            response = self._post("/api/generate", body)
            try:
                return json.loads(response.get("response") or "")
            except ValueError as e:
                if attempt:
                    raise OllamaError(f"the model did not return valid JSON: {e}") from e
                body["options"] |= {"temperature": 0.3}  # at 0 it would repeat the same answer
        raise AssertionError("unreachable")
