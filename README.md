# KeepBook

**On-device tax document sorter for small CPA and bookkeeping firms.** Built for the Build with Gemma: JustBuild hackathon — On-Device AI with Gemma 4 track.

Drop a folder of scanned tax documents (or snap them with a phone). Gemma 4 running locally classifies each document (W-2, 1099, K-1, 1098...), extracts key fields, groups documents into per-client bins, and maintains a per-client checklist of what's still missing. A human reviews and corrects every extraction before it's trusted. No client SSN, wage figure, or tax record ever leaves the machine.

**Why local:** a firm handling client tax data can't legally or safely paste it into a cloud AI tool. Local inference removes the third-party processor entirely.

See [PRD.md](PRD.md) for the full product spec, architecture, and evidence. **Working from this repo? Start at [docs/TASKS.md](docs/TASKS.md)** — the live task board with definitions of done; check tasks off only per its rules.

## Stack

- **Backend** — Python / FastAPI. Intake queue, classification + extraction via local Gemma 4 (`gemma4:e4b` through Ollama at `localhost:11434`), binning, checklist state.
- **Frontend** — plain HTML/CSS/JS, no build step. Capture UI + bin-review/checklist dashboard.
- **Eval** — `eval/` contains the kill-test scripts (`gen_w2.py`, `run_test.py`), the test-set generator (`gen_forms.py` — overlays fake data on official blank IRS PDFs in `eval/blank_forms/`), the phone-photo augmenter (`augment.py`), and the labeled test set itself (`eval/testset/`, 26 images + `labels.json`). All data synthetic.

## Models

Pull through [Ollama](https://ollama.com) — registry page: https://ollama.com/library/gemma4

```bash
ollama pull gemma4:e4b   # 9.6 GB — the deployed model (8.0B params, Q4_K_M, vision+tools)
ollama pull gemma4:e2b   # 7.2 GB — comparison model for the kill test only
```

Do NOT use `gemma4:cloud` (runs inference in Ollama's cloud — disqualifying for the On-Device track) and don't rely on `gemma4:latest` (tag can move; pin `e4b`).

Remote dev inference: the machine with the models runs `OLLAMA_HOST=0.0.0.0:11434 ollama serve`; other dev machines point at it over Tailscale (`http://<tailnet-name>:11434`). Demo-day inference runs entirely on the demo Mac.

**Runtime switch:** the backend talks to the model only through `backend/model_runtime.py`. `MODEL_RUNTIME=ollama` (default, verified) or `MODEL_RUNTIME=courier` (any OpenAI-compatible local server, e.g. [Courier OS](https://getcourier.ai) — supported in code, claimed nowhere until it passes the kill test). Same models, same prompts, and the eval harness honors the same switch. See [docs/API.md](docs/API.md).

## The kill test

Same synthetic W-2, two model sizes:

| Model | Fields correct | Failure |
|---|---|---|
| `gemma4:e2b` | 5/6 | Silently returned the wrong number for federal tax withheld — confident, clean JSON, wrong value |
| `gemma4:e4b` | 6/6 | None |

Reproduced 3x. This is why we ship `e4b` and why mandatory human review is a core feature, not a nicety.

## Run

```bash
# backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload  # (main.py in progress)

# eval
cd eval
python gen_w2.py
python run_test.py gemma4:e4b
```

Requires [Ollama](https://ollama.com) with `gemma4:e4b` pulled.

## Team

Keepbook — Vin Jones, Andrew.
