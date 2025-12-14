## ORATS Earnings Implied-Move Breach Web App

Small FastAPI + plain HTML app that:
- Accepts a US equity ticker
- Computes over the last 20 earnings (~5 years):
  - **Breach rate (%)**
  - **Average “above breach %”** (conditional on breach)
- **V2**: **Quarter Seasonality** breakdown (Q1–Q4) with per-quarter breach/near-breach metrics + a recommendation label
- **V2.1**: **Seasonality Score** per quarter (deltas vs baseline earnings behavior)
- Displays a detailed per-earnings table with implied vs realized moves and breach flags

### Architecture
- **Backend**: FastAPI
  - `GET /api/breach?ticker=XYZ&n=20&years=5&k=1.0`
  - ORATS token is read from **env var `ORATS_TOKEN`** (never sent to the browser)
  - Caching:
    - ORATS raw responses cached in-memory (TTL 6h)
    - `/api/breach` responses cached in-memory (TTL 6h)
- **Frontend**: `static/index.html` + minimal JS/CSS, served by FastAPI

### Setup

1) Create a `.env` locally (this repo includes `env.example` as a template):

```bash
cp env.example .env
```

Edit `.env` and set:
- `ORATS_TOKEN=...` (required)
- `PORT=8000` (optional)

2) Create a venv + install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Run

```bash
source .venv/bin/activate
PORT=${PORT:-8000}
uvicorn backend.app:app --host 0.0.0.0 --port "$PORT" --reload
```

Then open:
- `http://localhost:8000`

### API usage

Example:

```bash
curl "http://localhost:8000/api/breach?ticker=AAPL&n=20&years=5&k=1.0"
```

The response matches the JSON contract in `ORATS_Earnings_EM_Breach_Spec.txt`.

### Quarter Seasonality (V2)
The `/api/breach` response now includes a top-level `quarters` object with keys `Q1..Q4`, computed from the same filtered event set.

Per quarter we expose:
- breach stats (at the request’s `k`)
- near-breach rates at thresholds **0.8** and **0.9** based on \( \text{ratio} = \frac{\text{realizedMovePct}}{\text{impliedMovePct}} \)
- a simple **recommendation** label: `Tight` / `Standard` / `Wide` / `Avoid`

Note: recommendation uses a heuristic that evaluates **breach rate at k=1.0 internally** (so it stays comparable even if you change `k` in the request).

### Seasonality Score (V2.1)
The response also includes:
- a top-level `baseline` object (computed over the same usable event set)
- `quarters[Qx].seasonality` with deltas vs baseline:
  - `breach_delta_pp`
  - `ratio_delta`
  - `overshoot_delta_pp`
  - `z_breach`

Low sample handling:
- If `events_used < 3` for a quarter, `seasonality` fields are `null` and the recommendation is **`Avoid (low sample)`**.

### Tests

Tests are mocked (no ORATS calls) and cover:
- trading-day probing helper
- a small end-to-end breach + quarter aggregation calculation with mocked ORATS responses

Run:

```bash
pytest -q
```


