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


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Runtime shapes (docs/API.md)
# ---------------------------------------------------------------------------
def _extract_ollama(image_b64: str, prompt: str, model: str = None) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    # OLLAMA_PANSCAN=1 (default OFF — this path is the verified reference):
    # same multi-view packaging as the Courier path, for the matched-technique
    # bake-off arm. Fairness requires the identical crops + prompt hint.
    if os.environ.get("OLLAMA_PANSCAN", "0") == "1":
        images = _panscan_views(image_b64)
        prompt = (
            prompt
            + " You may receive multiple views of the SAME document: full page first, then zoomed crops."
        )
    else:
        images = [image_b64]
    payload = {
        "model": model or _model_name(),
        "prompt": prompt,
        "images": images,
        "stream": False,
        "options": {"temperature": 0},
    }
    out = _post_json(
        f"{host}/api/generate", payload, {"Content-Type": "application/json"}
    )
    return out.get("response", "")


def _panscan_views(image_b64: str) -> list:
    """Courier-only image packaging: full page + two half crops along the long
    axis (50px overlap). Courier's Gemma 4 processor downscales every image to
    one ~800px/280-token tile, which blurs dense tax forms; sending crops gives
    each region its own token budget (same idea as transformers' Gemma 3
    pan-and-scan). Verified: takes e2b from 3 wrong fields to 6/6 on w2_clean.
    Disable with COURIER_PANSCAN=0 for A/B runs.
    """
    import base64
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
    w, h = im.size
    o = 50
    if min(w, h) > 1400:
        # both dimensions dense (e.g. full-page K-1): 2x2 grid so each crop
        # lands near the processor's native tile size
        crops = [
            im.crop((0, 0, w // 2 + o, h // 2 + o)),
            im.crop((w // 2 - o, 0, w, h // 2 + o)),
            im.crop((0, h // 2 - o, w // 2 + o, h)),
            im.crop((w // 2 - o, h // 2 - o, w, h)),
        ]
    elif w >= h:
        crops = [im.crop((0, 0, w // 2 + o, h)), im.crop((w // 2 - o, 0, w, h))]
    else:
        crops = [im.crop((0, 0, w, h // 2 + o)), im.crop((0, h // 2 - o, w, h))]
    views = [image_b64]
    for c in crops:
        buf = io.BytesIO()
        c.save(buf, "PNG")
        views.append(base64.b64encode(buf.getvalue()).decode())
    return views


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
    if os.environ.get("COURIER_PANSCAN", "1") != "0":
        views = _panscan_views(image_b64)
        prompt = (
            prompt
            + " You may receive multiple views of the SAME document: full page first, then zoomed crops."
        )
    else:
        views = [image_b64]
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + v}}
        for v in views
    ]
    payload = {
        "model": model or _model_name(),
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }
    # Courier evicts/reloads models under memory pressure; a call that lands
    # mid-swap gets a transient 500 or times out while the model pages in.
    # Retry a few times so one flake doesn't kill a 29-doc eval run.
    import time
    import urllib.error

    last_err = None
    for attempt in range(4):
        try:
            out = _post_json(f"{base}/chat/completions", payload, headers)
            return out["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(12)
    raise last_err


def extract(image_b64: str, prompt: str, model: str = None) -> str:
    """Send one image + prompt to the configured runtime, return raw text.

    Optional model= overrides MODEL_NAME for this one call (used by the CASCADE
    strategy to route classify to a smaller model). Defaults to MODEL_NAME.
    """
    runtime = _runtime()
    if runtime == "courier":
        return _extract_courier(image_b64, prompt, model=model)
    return _extract_ollama(image_b64, prompt, model=model)


# ---------------------------------------------------------------------------
# Text-only call (docs/API.md "Model call (backend internal)" — nudge draft).
# Same runtime selection + env vars as extract(), no image payload. Kept in
# this module only, per the one-adapter discipline (grep-verified in CI/tests:
# no other backend file may hardcode a model URL).
# ---------------------------------------------------------------------------
def _generate_text_ollama(prompt: str, model: str = None) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    payload = {
        "model": model or _model_name(),
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    out = _post_json(
        f"{host}/api/generate", payload, {"Content-Type": "application/json"}, timeout=30
    )
    return out.get("response", "")


def _generate_text_courier(prompt: str, model: str = None) -> str:
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
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    out = _post_json(f"{base}/chat/completions", payload, headers, timeout=30)
    return out["choices"][0]["message"]["content"]


def generate_text(prompt: str, model: str = None) -> str:
    """Text-only call through the configured runtime (no image). Used by the
    nudge-draft endpoint. Same MODEL_RUNTIME/MODEL_NAME env selection as
    extract(); callers must treat any exception (timeout, connection error,
    non-JSON body) as "no draft" and fall back — this function never retries
    and never swallows errors itself.
    """
    runtime = _runtime()
    if runtime == "courier":
        return _generate_text_courier(prompt, model=model)
    return _generate_text_ollama(prompt, model=model)


if __name__ == "__main__":
    # Smoke: MODEL_RUNTIME=ollama python model_runtime.py <image_path>
    import base64
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "../eval/w2_test.png"
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    print(extract(b64, 'Return STRICT JSON: {"doc_type": "..."} for this document.'))
