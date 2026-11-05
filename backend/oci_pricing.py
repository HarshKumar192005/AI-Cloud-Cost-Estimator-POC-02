import os
import io
import json
from typing import List, Dict, Any
import requests
from openpyxl import load_workbook
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ORACLE_PRICE_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products?currencyCode=USD"

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY not set. Copy backend/.env.example to backend/.env and fill it in."
    )

client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Prompt: the AI now receives the real unitPrice for every candidate product
# and is responsible for BOTH determining qty and multiplying it out into a
# final "cost". Python no longer re-looks-up the price by (displayName,
# metricName) after the fact - that lookup was fragile because the prompt
# previously told the model to relabel some line items (e.g. "VM-Storage-
# block") in ways that don't exactly match the Oracle price list, causing
# "Unresolved product/metric" warnings even when the estimate was otherwise
# correct. Now Gemini is explicitly told to copy unitPrice verbatim from the
# Available Products list and do the arithmetic itself, so nothing needs to
# match afterwards - Python just sums the "cost" values it returns.
#
# This single prompt is sent ONCE per /api/estimate request, covering ALL
# requirements from the uploaded sheet in one Gemini call.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an Oracle Cloud Infrastructure pricing expert.
You will be given a list of Requirements (each with a "category" and
"details") and a list of Available Products (each with displayName,
metricName, unitPrice, and the "category" it was matched against).

For EACH requirement, work out which product(s)/metric(s) are needed, how
much of each metric is consumed, and the resulting pre-tax cost. Return
ONLY a JSON array, no markdown fences, no commentary, covering ALL
requirements together:

[
  {
    "category": "<the requirement's category, copied exactly>",
    "displayName": "<label for this line item - can be descriptive, does NOT need to match Available Products verbatim>",
    "metricName": "<the metric being consumed, e.g. 'OCPU Per Hour'>",
    "unitPrice": <the unitPrice copied EXACTLY from the Available Products entry you used>,
    "qty": <numeric quantity of the metric consumed>,
    "calculation": "<plain-English arithmetic showing the full derivation, e.g. '744 hrs/month * 2 OCPUs * 5.00 USD = 7440'>",
    "cost": <qty * unitPrice, computed by you, numeric, pre-tax>
  }
]

Rules:
- Only use unitPrice values that come from the Available Products supplied
  for that requirement's category - never invent a price. Copy it exactly
  as given, then do the multiplication yourself.
- Calculate as per metric; if it's free first x units make sure you show the substraction in calculation
- "cost" must equal qty * unitPrice exactly (pre-tax). Do not round unless
  the arithmetic naturally produces a rounded figure.
- For VM requirements, select only E-series VM shapes; storage is Block
  Storage - label it clearly as VM storage in "displayName" so it's
  distinguishable from standalone Block Storage, but you do not need to
  force an exact product-list string match. Include an appropriate OS
  image / licensing line item where relevant.
- Avoid Cloud@Customer products whenever a non-Cloud@Customer alternative
  exists.
- Avoid Exadata unless specified.
- Do NOT apply or mention tax/GST anywhere - qty, unitPrice, and cost are
  all pre-tax.
- Every requirement in the input must be addressed by at least one item
  in the output (unless truly no candidate product fits it).
- Block Storage PU = GB*20 for high performance else *10.
"""

_products_cache: List[Dict[str, Any]] = []


def fetch_products(force: bool = False) -> List[Dict[str, Any]]:
    global _products_cache
    if force or not _products_cache:
        resp = requests.get(ORACLE_PRICE_URL, timeout=30)
        resp.raise_for_status()
        _products_cache = resp.json()["items"]
    return _products_cache


def build_lookup(products) -> Dict[tuple, float]:
    lk = {}
    for p in products:
        locs = p.get("currencyCodeLocalizations") or []
        if not locs:
            continue
        prices = locs[0].get("prices") or []
        if not prices:
            continue
        lk[(p["displayName"], p["metricName"])] = prices[0]["value"]
    return lk


def relevant_products(category_name: str, products, lookup, limit: int = None):
    target = category_name.lower().strip()
    results = []
    for p in products:
        svc_cat = p.get("serviceCategory", "").lower().strip()
        if target in svc_cat and 'cloud@customer' not in svc_cat:
            key = (p["displayName"], p["metricName"])
            if key in lookup:
                results.append({
                    "displayName": p["displayName"],
                    "metricName": p["metricName"],
                    "unitPrice": lookup[key],
                })
    return results[:limit] if limit else results


def excel_to_requirements(file_bytes):
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    output = []
    i = 0
    while i < len(rows):
        row = rows[i]
        # Detect category row
        if (
            row
            and row[0]
            and str(row[0]).strip().startswith("Category:")
        ):
            category = str(row[0]).replace("Category:", "").strip()
            # Header row
            if i + 1 >= len(rows):
                break
            headers = [
                str(h).strip() if h is not None else None
                for h in rows[i + 1]
            ]
            details = []
            j = i + 2
            while j < len(rows):
                current = rows[j]
                # Stop at next category
                if (
                    current
                    and current[0]
                    and str(current[0]).strip().startswith("Category:")
                ):
                    break
                # Skip blank rows
                if not any(cell is not None for cell in current):
                    j += 1
                    continue
                item = {}
                for k, header in enumerate(headers):
                    if (
                        header
                        and k < len(current)
                        and current[k] is not None
                    ):
                        item[header] = current[k]
                if item:
                    details.append(item)
                j += 1
            output.append({
                "category": category,
                "details": details
            })
            i = j
        else:
            i += 1

    return output


def build_batch_candidates(reqs, products, lookup, limit_per_req: int = 150):
    """
    For each requirement, find relevant candidate products (with their real
    unitPrice attached) and tag them with the category so Gemini can map its
    output back correctly and multiply out the cost itself.
    Returns a flat, deduplicated list.
    """
    seen = set()
    batch_candidates = []
    for req in reqs:
        category = req.get("category", "")
        cands = relevant_products(category, products, lookup, limit=limit_per_req)
        for c in cands:
            key = (category, c["displayName"], c["metricName"])
            if key in seen:
                continue
            seen.add(key)
            batch_candidates.append({
                "category": category,
                "displayName": c["displayName"],
                "metricName": c["metricName"],
                "unitPrice": c["unitPrice"],
            })
    return batch_candidates


def ask_gemini_batch(reqs: List[Dict[str, Any]], candidates: List[Dict[str, Any]]):
    """
    Single Gemini call covering ALL requirements + ALL matched candidates
    (with real unitPrice) for the entire uploaded sheet. Gemini returns the
    final "cost" per line item already multiplied out.
    """
    prompt = (
        SYSTEM_PROMPT
        + "\nRequirements:\n" + json.dumps(reqs, indent=2, default=str)
        + "\nAvailable Products:\n" + json.dumps(candidates, indent=2, default=str)
    )
    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    print(prompt)
    return json.loads(r.text)


def run_oci_estimate(file_bytes: bytes) -> Dict[str, Any]:
    """
    Full OCI estimation pipeline for a given uploaded Excel file's raw bytes.
    Mirrors the original /api/estimate endpoint logic exactly, just extracted
    into a reusable function so main.py can call it alongside Azure.
    """
    reqs = excel_to_requirements(file_bytes)

    if not reqs:
        return {"report": [], "total": 0, "warnings": ["No requirements found in the uploaded sheet"]}

    products = fetch_products()
    lookup = build_lookup(products)

    warnings: List[str] = []
    batch_candidates = build_batch_candidates(reqs, products, lookup)

    # Flag requirements with zero matched candidates up front
    matched_categories = {c["category"] for c in batch_candidates}
    for req in reqs:
        cat = req.get("category", "")
        if cat not in matched_categories:
            warnings.append(f"No matching OCI products found for '{cat}'")

    if not batch_candidates:
        return {"report": [], "total": 0, "warnings": warnings}

    items = ask_gemini_batch(reqs, batch_candidates)

    report = []
    for it in items:
        try:
            cost = round(float(it.get("cost", 0)), 2)
        except (TypeError, ValueError):
            warnings.append(
                f"Could not parse cost for '{it.get('displayName', '?')}'; skipped"
            )
            continue

        report.append({
            "category": it.get("category", ""),
            "product": it.get("displayName", ""),
            "metric": it.get("metricName", ""),
            "calculation": it.get("calculation", ""),
            "cost": cost,
        })

    total = round(sum(r["cost"] for r in report), 2)
    return {"report": report, "total": total, "warnings": warnings}
# import os
# import io
# import json
# from typing import List, Dict, Any
# import requests
# from openpyxl import load_workbook
# from dotenv import load_dotenv
# from google import genai
# from google.genai import types

# load_dotenv()

# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
# ORACLE_PRICE_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products?currencyCode=USD"

# if not GEMINI_API_KEY:
#     raise RuntimeError(
#         "GEMINI_API_KEY not set. Copy backend/.env.example to backend/.env and fill it in."
#     )

# client = genai.Client(api_key=GEMINI_API_KEY)

# # ---------------------------------------------------------------------------
# # Prompt: the AI now receives the real unitPrice for every candidate product
# # and is responsible for BOTH determining qty and multiplying it out into a
# # final "cost". Python no longer re-looks-up the price by (displayName,
# # metricName) after the fact - that lookup was fragile because the prompt
# # previously told the model to relabel some line items (e.g. "VM-Storage-
# # block") in ways that don't exactly match the Oracle price list, causing
# # "Unresolved product/metric" warnings even when the estimate was otherwise
# # correct. Now Gemini is explicitly told to copy unitPrice verbatim from the
# # Available Products list and do the arithmetic itself, so nothing needs to
# # match afterwards - Python just sums the "cost" values it returns.
# #
# # This single prompt is sent ONCE per /api/estimate request, covering ALL
# # requirements from the uploaded sheet in one Gemini call.
# # ---------------------------------------------------------------------------
# SYSTEM_PROMPT = """You are an Oracle Cloud Infrastructure pricing expert.
# You will be given a list of Requirements (each with a "category" and
# "details") and a list of Available Products (each with displayName,
# metricName, unitPrice, and the "category" it was matched against).

# For EACH requirement, work out which product(s)/metric(s) are needed, how
# much of each metric is consumed, and the resulting pre-tax cost. Return
# ONLY a JSON array, no markdown fences, no commentary, covering ALL
# requirements together:

# [
#   {
#     "category": "<the requirement's category, copied exactly>",
#     "displayName": "<label for this line item - can be descriptive, does NOT need to match Available Products verbatim>",
#     "metricName": "<the metric being consumed, e.g. 'OCPU Per Hour'>",
#     "unitPrice": <the unitPrice copied EXACTLY from the Available Products entry you used>,
#     "qty": <numeric quantity of the metric consumed>,
#     "calculation": "<plain-English arithmetic showing the full derivation, e.g. '744 hrs/month * 2 OCPUs * 5.00 USD = 7440'>",
#     "cost": <qty * unitPrice, computed by you, numeric, pre-tax>
#   }
# ]

# Rules:
# - Only use unitPrice values that come from the Available Products supplied
#   for that requirement's category - never invent a price. Copy it exactly
#   as given, then do the multiplication yourself.
# - "cost" must equal qty * unitPrice exactly (pre-tax). Do not round unless
#   the arithmetic naturally produces a rounded figure.
# - For VM requirements, select only E-series VM shapes; storage is Block
#   Storage - label it clearly as VM storage in "displayName" so it's
#   distinguishable from standalone Block Storage, but you do not need to
#   force an exact product-list string match. Include an appropriate OS
#   image / licensing line item where relevant.
# - Avoid Cloud@Customer products whenever a non-Cloud@Customer alternative
#   exists.
# - Avoid Exadata unless specified.
# - Do NOT apply or mention tax/GST anywhere - qty, unitPrice, and cost are
#   all pre-tax.
# - Every requirement in the input must be addressed by at least one item
#   in the output (unless truly no candidate product fits it).
# - Block Storage PU = GB*20 for high performance else *10.
# """

# _products_cache: List[Dict[str, Any]] = []


# def fetch_products(force: bool = False) -> List[Dict[str, Any]]:
#     global _products_cache
#     if force or not _products_cache:
#         resp = requests.get(ORACLE_PRICE_URL, timeout=30)
#         resp.raise_for_status()
#         _products_cache = resp.json()["items"]
#     return _products_cache


# def build_lookup(products) -> Dict[tuple, float]:
#     lk = {}
#     for p in products:
#         locs = p.get("currencyCodeLocalizations") or []
#         if not locs:
#             continue
#         prices = locs[0].get("prices") or []
#         if not prices:
#             continue
#         lk[(p["displayName"], p["metricName"])] = prices[0]["value"]
#     return lk


# def relevant_products(category_name: str, products, lookup, limit: int = None):
#     target = category_name.lower().strip()
#     results = []
#     for p in products:
#         svc_cat = p.get("serviceCategory", "").lower().strip()
#         if target in svc_cat and 'cloud@customer' not in svc_cat:
#             key = (p["displayName"], p["metricName"])
#             if key in lookup:
#                 results.append({
#                     "displayName": p["displayName"],
#                     "metricName": p["metricName"],
#                     "unitPrice": lookup[key],
#                 })
#     return results[:limit] if limit else results


# def excel_to_requirements(file_bytes):
#     wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
#     ws = wb.active
#     rows = list(ws.iter_rows(values_only=True))
#     output = []
#     i = 0
#     while i < len(rows):
#         row = rows[i]
#         # Detect category row
#         if (
#             row
#             and row[0]
#             and str(row[0]).strip().startswith("Category:")
#         ):
#             category = str(row[0]).replace("Category:", "").strip()
#             # Header row
#             if i + 1 >= len(rows):
#                 break
#             headers = [
#                 str(h).strip() if h is not None else None
#                 for h in rows[i + 1]
#             ]
#             details = []
#             j = i + 2
#             while j < len(rows):
#                 current = rows[j]
#                 # Stop at next category
#                 if (
#                     current
#                     and current[0]
#                     and str(current[0]).strip().startswith("Category:")
#                 ):
#                     break
#                 # Skip blank rows
#                 if not any(cell is not None for cell in current):
#                     j += 1
#                     continue
#                 item = {}
#                 for k, header in enumerate(headers):
#                     if (
#                         header
#                         and k < len(current)
#                         and current[k] is not None
#                     ):
#                         item[header] = current[k]
#                 if item:
#                     details.append(item)
#                 j += 1
#             output.append({
#                 "category": category,
#                 "details": details
#             })
#             i = j
#         else:
#             i += 1

#     return output


# def build_batch_candidates(reqs, products, lookup, limit_per_req: int = 150):
#     """
#     For each requirement, find relevant candidate products (with their real
#     unitPrice attached) and tag them with the category so Gemini can map its
#     output back correctly and multiply out the cost itself.
#     Returns a flat, deduplicated list.
#     """
#     seen = set()
#     batch_candidates = []
#     for req in reqs:
#         category = req.get("category", "")
#         cands = relevant_products(category, products, lookup, limit=limit_per_req)
#         for c in cands:
#             key = (category, c["displayName"], c["metricName"])
#             if key in seen:
#                 continue
#             seen.add(key)
#             batch_candidates.append({
#                 "category": category,
#                 "displayName": c["displayName"],
#                 "metricName": c["metricName"],
#                 "unitPrice": c["unitPrice"],
#             })
#     return batch_candidates


# def ask_gemini_batch(reqs: List[Dict[str, Any]], candidates: List[Dict[str, Any]]):
#     """
#     Single Gemini call covering ALL requirements + ALL matched candidates
#     (with real unitPrice) for the entire uploaded sheet. Gemini returns the
#     final "cost" per line item already multiplied out.
#     """
#     prompt = (
#         SYSTEM_PROMPT
#         + "\nRequirements:\n" + json.dumps(reqs, indent=2, default=str)
#         + "\nAvailable Products:\n" + json.dumps(candidates, indent=2, default=str)
#     )
#     r = client.models.generate_content(
#         model=GEMINI_MODEL,
#         contents=prompt,
#         config=types.GenerateContentConfig(response_mime_type="application/json"),
#     )
#     print(prompt)
#     return json.loads(r.text)


# def run_oci_estimate(file_bytes: bytes) -> Dict[str, Any]:
#     """
#     Full OCI estimation pipeline for a given uploaded Excel file's raw bytes.
#     Mirrors the original /api/estimate endpoint logic exactly, just extracted
#     into a reusable function so main.py can call it alongside Azure.
#     """
#     reqs = excel_to_requirements(file_bytes)

#     if not reqs:
#         return {"report": [], "total": 0, "warnings": ["No requirements found in the uploaded sheet"]}

#     products = fetch_products()
#     lookup = build_lookup(products)

#     warnings: List[str] = []
#     batch_candidates = build_batch_candidates(reqs, products, lookup)

#     # Flag requirements with zero matched candidates up front
#     matched_categories = {c["category"] for c in batch_candidates}
#     for req in reqs:
#         cat = req.get("category", "")
#         if cat not in matched_categories:
#             warnings.append(f"No matching OCI products found for '{cat}'")

#     if not batch_candidates:
#         return {"report": [], "total": 0, "warnings": warnings}

#     items = ask_gemini_batch(reqs, batch_candidates)

#     report = []
#     for it in items:
#         try:
#             cost = round(float(it.get("cost", 0)), 2)
#         except (TypeError, ValueError):
#             warnings.append(
#                 f"Could not parse cost for '{it.get('displayName', '?')}'; skipped"
#             )
#             continue

#         report.append({
#             "category": it.get("category", ""),
#             "product": it.get("displayName", ""),
#             "metric": it.get("metricName", ""),
#             "calculation": it.get("calculation", ""),
#             "cost": cost,
#         })

#     total = round(sum(r["cost"] for r in report), 2)
#     return {"report": report, "total": total, "warnings": warnings}
