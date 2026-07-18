"""Single model adapter for KeepBook — the ONLY place a model URL lives.

Contract: docs/API.md "Model call (backend internal)".
Exposes exactly one entry point: extract(image_b64, prompt) -> str.
Runtime is selected by the MODEL_RUNTIME env var (ollama default | courier).

Both call shapes are implemented with plain urllib (no SDK) so the ollama path
is byte-for-byte the verified reference in eval/run_test.py.
"""

import json
import os
import urllib.request

# ---------------------------------------------------------------------------
# .env loader (tiny, hand-rolled). Loads backend/.env if present without
# clobbering anything already set in the real environment.
# ---------------------------------------------------------------------------
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env(path: str = _ENV_PATH) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env()


# ---------------------------------------------------------------------------
# Config helpers (read at call time so env changes / tests take effect)
# ---------------------------------------------------------------------------
def _runtime() -> str:
    return os.environ.get("MODEL_RUNTIME", "ollama").lower()


def _model_name() -> str:
    return os.environ.get("MODEL_NAME", "gemma4:e4b")


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Runtime shapes (docs/API.md)
# ---------------------------------------------------------------------------
def _extract_ollama(image_b64: str, prompt: str, model: str = None) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    payload = {
        "model": model or _model_name(),
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0},
    }
    out = _post_json(
        f"{host}/api/generate", payload, {"Content-Type": "application/json"}
    )
    return out.get("response", "")


def _extract_courier(image_b64: str, prompt: str, model: str = None) -> str:
    base = os.environ.get("COURIER_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "MODEL_RUNTIME=courier but COURIER_BASE_URL is unset (see backend/.env)"
        )
    key = os.environ.get("COURIER_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model or _model_name(),
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + image_b64},
                    },
                ],
            }
        ],
    }
    out = _post_json(f"{base}/chat/completions", payload, headers)
    return out["choices"][0]["message"]["content"]


def extract(image_b64: str, prompt: str, model: str = None) -> str:
    """Send one image + prompt to the configured runtime, return raw text.

    Optional model= overrides MODEL_NAME for this one call (used by the CASCADE
    strategy to route classify to a smaller model). Defaults to MODEL_NAME.
    """
    runtime = _runtime()
    if runtime == "courier":
        return _extract_courier(image_b64, prompt, model=model)
    return _extract_ollama(image_b64, prompt, model=model)


if __name__ == "__main__":
    # Smoke: MODEL_RUNTIME=ollama python model_runtime.py <image_path>
    import base64
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "../eval/w2_test.png"
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    print(extract(b64, 'Return STRICT JSON: {"doc_type": "..."} for this document.'))
