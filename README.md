
# Cloud Cost Estimator (Azure + OCI)

One requirements sheet in, two side-by-side cost estimates out: your original
Azure calculator logic and your original OCI/Gemini calculator logic, both
untouched, just wired together behind one FastAPI app and rendered as two
boxes in a Next.js + Tailwind frontend.

## Layout

```
backend/
  azure_pricing.py   # AzurePricingCalculator - logic unchanged
  oci_pricing.py      # OCI + Gemini pipeline - logic unchanged, FastAPI routes stripped out
  main.py             # Unified FastAPI app: /api/estimate?cloud=azure|oci|both
  requirements.txt
  .env.example

frontend/
  app/page.js         # Upload form + Azure box + OCI box side by side
  app/layout.js
  app/globals.css
  tailwind.config.js
  package.json
```

## How the sheets work

Both engines already expected the same Excel shape (rows starting with
`Category: <name>`, followed by a header row, followed by data rows), so
`excel_to_requirements()` from `oci_pricing.py` is reused for both the Azure
and OCI paths — you upload one file and both calculators parse it themselves.

Azure understands these categories: `Compute`, `Storage` (with a `Type` of
`Block`/`Object`/`File`), `Flexible Load Balancer`,
`Networking - FastConnect`, `Application Development - API Management`.

OCI's categories are free-form — Gemini matches them against Oracle's
`serviceCategory` field itself.

## Running the backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GEMINI_API_KEY
uvicorn main:app --reload --port 8000
```

Endpoints:
- `GET  /api/health`
- `POST /api/refresh-products` — refreshes the cached OCI price list
- `POST /api/estimate?cloud=both` — multipart file upload (`file`), `cloud` is
  `azure`, `oci`, or `both` (default). Returns:

```json
{
  "cloud": "both",
  "azure": { "total_monthly_cost": 0, "currency": "USD", "breakdown": [], "warnings": [] },
  "oci": { "report": [], "total": 0, "warnings": [] }
}
```

## Running the frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local   # point at your backend if not localhost:8000
npm run dev
```

Open http://localhost:3000, upload your requirements `.xlsx`, and the Azure
and OCI estimates render as two boxes side by side (stacked on mobile).
# Cloud-Cost-Estimator-

