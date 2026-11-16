"""A minimal Ollama client: one prompt in, one JSON object out.

``OllamaClient`` posts to the local Ollama HTTP API with JSON output and returns the
parsed object; ``OllamaError`` covers connection and format problems. See docs/ai.md.

Usage:
    import job_radar.ai.ollama as ai_ollama
"""

import json
import typing as t
import urllib.error
import urllib.request


class OllamaError(Exception):
    """Ollama could not be reached or did not return JSON."""

    pass


class OllamaClient:
    """Client of the local Ollama HTTP API for one model."""

    def __init__(self, url: str, model: str, timeout: float = 120):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, t.Any]) -> dict[str, t.Any]:
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

    def generate_json(self, prompt: str, schema: dict[str, t.Any]) -> dict[str, t.Any]:
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
                # at 0 it would repeat the same answer
                body["options"] |= {"temperature": 0.3}
        raise AssertionError("unreachable")
