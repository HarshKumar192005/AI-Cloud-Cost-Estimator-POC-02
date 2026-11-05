"""
aws_pricing.py - Gemini mapping + AWS pricing + Gemini finalize (BATCHED)
--------------------------------------------------------------------------
Same 2-Gemini-calls-total pipeline as test_full_pipeline.py, wrapped as
run_aws_estimate(file_bytes) so it can be called from main.py exactly like
run_oci_estimate / run_azure_estimate.
"""

import json
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from google import genai
from google.genai import types

from oci_pricing import excel_to_requirements  # reuse the same sheet -> requirements parser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PRICING_CLIENT_REGION = "us-east-1"
TARGET_REGION_CODE = "us-east-1"
GEMINI_MODEL = "gemini-3.5-flash"
HOURS_PER_MONTH = 744  # 31-day month

REGION_NAME_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "eu-west-1": "EU (Ireland)",
}

pricing_client = boto3.client("pricing", region_name=PRICING_CLIENT_REGION)

# NOTE: pulled the hardcoded key out into an env var — same as the OCI/Azure
# modules expect their creds to come from the environment / aws configure.
gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def region_display_name(region_code: str) -> str:
    return REGION_NAME_MAP.get(region_code, region_code)


# ---------------------------------------------------------------------------
# GEMINI CALL #1 (batched) - all items -> one mapping per item
# ---------------------------------------------------------------------------

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "description": "One mapping object per input item, IN THE SAME ORDER as the "
                            "input items were given.",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {
                        "type": "integer",
                        "description": "0-based index of this item in the flattened input list.",
                    },
                    "aws_service_code": {
                        "type": "string",
                        "description": "Exact AWS Pricing API ServiceCode, e.g. AmazonEC2, "
                                        "AmazonS3, AmazonEFS, AmazonRDS, AmazonDynamoDB, AWSELB.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "TERM_MATCH filters for pricing.get_products. Do NOT "
                                        "include 'location'. Pick the single most important cost "
                                        "dimension for the item (e.g. VM -> compute-hours; "
                                        "storage -> GB).",
                        "properties": {},
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Multiplier so total = unit_price * quantity "
                                        f"(e.g. {HOURS_PER_MONTH} hrs/month x count for continuous "
                                        "compute, GB for storage).",
                    },
                    "quantity_unit": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["item_index", "aws_service_code", "filters", "quantity",
                             "quantity_unit", "reasoning"],
            },
        },
    },
    "required": ["mappings"],
}

MAPPING_SYSTEM_INSTRUCTIONS = f"""You are an AWS pricing mapping assistant.

You will receive a JSON array of infrastructure requirement items (any cloud vendor's
terminology - OCI, Azure, GCP, generic - not necessarily AWS's own). Each item has an
"item_index" and a "category" plus its original fields.

For EVERY item, pick the ONE AWS service/product that is the primary cost driver, and
produce the exact `pricing.get_products` TERM_MATCH filters (AWS Price List Query API)
needed to look up its on-demand USD rate, plus the quantity multiplier to turn that unit
rate into a total monthly cost. Return one mapping object per item, in the SAME ORDER,
using the given item_index so the caller can match mappings back to items.

Guidance by item shape:
- VM/instance -> AmazonEC2, productFamily "Compute Instance". Price the compute-hours only.
- Block storage -> AmazonEC2, productFamily "Storage", volumeType "General Purpose" (EBS,
  billed under EC2). Price the GB.
- Object storage -> AmazonS3, productFamily "Storage", storageClass "General Purpose". Price
  the GB (ignore request counts).
- File storage -> AmazonEFS, productFamily "Storage". Price the GB.

Rules:
- Pick real AWS ServiceCodes and real, valid filter field names/values
  (AmazonEC2: instanceType/operatingSystem/tenancy/preInstalledSw/capacitystatus/
  productFamily/licenseModel; AmazonS3: productFamily/storageClass/volumeType;
  AmazonEFS: productFamily/storageClass; AmazonRDS: instanceType/databaseEngine/
  deploymentOption).
- If vCPU/memory are given for compute, pick a plausible current-generation instance type
  sized to at least meet those specs. If a vendor-specific unit like OCPU is given, convert
  it to vCPUs first (1 OCPU = 2 vCPUs) before sizing.
- Windows requires licenseModel "No License required" in the filters; Linux does not.
- Never include "location" in filters - it's added by the caller.
- Assume {HOURS_PER_MONTH} hours/month for anything billed hourly and running continuously.
- Respond ONLY via the structured schema - no prose outside it.
"""


def gemini_map_all_requirements(flattened_items: list[dict]) -> list[dict]:
    prompt = json.dumps({"items": flattened_items})
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=MAPPING_SYSTEM_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=MAPPING_SCHEMA,
            temperature=0.1,
        ),
    )
    result = json.loads(response.text)
    return result.get("mappings", [])


# ---------------------------------------------------------------------------
# AWS CALLS (looped, one per item - not batchable via Pricing API)
# ---------------------------------------------------------------------------

def get_price_per_unit_and_meta(price_list_item: dict) -> Optional[dict]:
    try:
        terms = price_list_item["terms"]["OnDemand"]
        first_term = next(iter(terms.values()))
        price_dims = first_term["priceDimensions"]
        first_dim = next(iter(price_dims.values()))
        return {
            "unit_price_usd": float(first_dim["pricePerUnit"]["USD"]),
            "unit": first_dim.get("unit"),
            "description": first_dim.get("description"),
        }
    except (KeyError, StopIteration, ValueError):
        return None


def query_pricing(service_code: str, filters: dict, max_results: int = 10) -> list[dict]:
    aws_filters = [
        {"Type": "TERM_MATCH", "Field": k, "Value": str(v)}
        for k, v in filters.items() if v is not None
    ]
    try:
        resp = pricing_client.get_products(
            ServiceCode=service_code,
            Filters=aws_filters,
            MaxResults=max_results,
        )
        return [json.loads(p) for p in resp.get("PriceList", [])]
    except ClientError as e:
        print(f"[Pricing API error] service={service_code} filters={filters}: {e}")
        return []


# ---------------------------------------------------------------------------
# GEMINI CALL #2 (batched) - all items' raw candidates -> one final cost per item
# ---------------------------------------------------------------------------

FINALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "description": "One result object per item, IN THE SAME ORDER given, matched "
                            "by item_index.",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {"type": "integer"},
                    "product": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "sku_attributes": {"type": "object", "properties": {}},
                            "unit_price_usd": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                        "required": ["description", "sku_attributes", "unit_price_usd", "unit"],
                    },
                    "calculation": {
                        "type": "string",
                        "description": "e.g. '$0.192/hr x 744 hrs = $142.85'",
                    },
                    "monthly_cost_usd": {"type": "number"},
                },
                "required": ["item_index", "product", "calculation", "monthly_cost_usd"],
            },
        },
    },
    "required": ["results"],
}

FINALIZE_SYSTEM_INSTRUCTIONS = """You are given a JSON array. Each entry has an item_index,
the original requirement item, the filters used to query AWS Pricing API, the quantity, and
a list of candidate AWS products returned by that query (each with attributes, on-demand
unit price, and billing unit).

For EVERY entry: pick the ONE candidate that best matches the filters/intent (ignore
obviously wrong variants such as Spot/Reserved pricing, mismatched OS, or mismatched
tenancy). Then compute:
    monthly_cost_usd = unit_price_usd * quantity

Round monthly_cost_usd to 2 decimal places. Show the arithmetic in `calculation` as a plain
string. Return one result object per entry, in the same order, using item_index to match
back. Respond ONLY via the schema.
"""


def gemini_finalize_all(finalize_payload: list[dict]) -> list[dict]:
    prompt = json.dumps({"items": finalize_payload})
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=FINALIZE_SYSTEM_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=FINALIZE_SCHEMA,
            temperature=0.1,
        ),
    )
    result = json.loads(response.text)
    return result.get("results", [])


# ---------------------------------------------------------------------------
# Orchestration: 2 Gemini calls total, AWS calls looped per item
# ---------------------------------------------------------------------------

def price_all_requirements(requirements: list[dict]) -> list[dict]:
    flattened = []
    for block in requirements:
        for detail in block["details"]:
            flattened.append({
                "item_index": len(flattened),
                "category": block["category"],
                **detail,
            })

    if not flattened:
        return []

    mappings = gemini_map_all_requirements(flattened)
    mappings_by_index = {m["item_index"]: m for m in mappings}

    finalize_payload = []
    no_candidates_results = {}

    for item in flattened:
        idx = item["item_index"]
        mapping = mappings_by_index.get(idx)
        if mapping is None:
            no_candidates_results[idx] = {
                "item_index": idx,
                "product": None,
                "calculation": None,
                "monthly_cost_usd": None,
                "warning": "Gemini did not return a mapping for this item.",
            }
            continue

        filters = dict(mapping.get("filters") or {})
        filters["location"] = region_display_name(TARGET_REGION_CODE)

        raw_candidates = query_pricing(mapping["aws_service_code"], filters)

        if not raw_candidates:
            no_candidates_results[idx] = {
                "item_index": idx,
                "product": None,
                "calculation": None,
                "monthly_cost_usd": None,
                "warning": "No matching AWS product found - filters may need adjusting.",
            }
            continue

        candidates_meta = []
        for p in raw_candidates:
            meta = get_price_per_unit_and_meta(p)
            if meta is None:
                continue
            candidates_meta.append({
                "attributes": p.get("product", {}).get("attributes", {}),
                **meta,
            })

        finalize_payload.append({
            "item_index": idx,
            "requirement_item": item,
            "filters_used": filters,
            "quantity": mapping.get("quantity"),
            "quantity_unit": mapping.get("quantity_unit"),
            "candidates": candidates_meta,
        })

    finalized_results = []
    if finalize_payload:
        finalized_results = gemini_finalize_all(finalize_payload)

    finalized_by_index = {r["item_index"]: r for r in finalized_results}

    output = []
    for item in flattened:
        idx = item["item_index"]
        mapping = mappings_by_index.get(idx, {})

        if idx in no_candidates_results:
            entry = no_candidates_results[idx]
        else:
            final = finalized_by_index.get(idx, {
                "product": None, "calculation": None, "monthly_cost_usd": None,
            })
            entry = {**final, "warning": None}

        output.append({
            "item_index": idx,
            "category": item["category"],
            "requested": {k: v for k, v in item.items() if k not in ("item_index", "category")},
            "aws_service_code": mapping.get("aws_service_code"),
            "quantity": mapping.get("quantity"),
            "quantity_unit": mapping.get("quantity_unit"),
            "reasoning": mapping.get("reasoning"),
            **{k: v for k, v in entry.items() if k != "item_index"},
        })

    return output


# ---------------------------------------------------------------------------
# Public entry point - matches run_oci_estimate(file_bytes) signature
# ---------------------------------------------------------------------------

def run_aws_estimate(file_bytes: bytes) -> dict:
    """
    Runs the Gemini + AWS Pricing pipeline against the same 'Category: ...'
    shaped Excel sheet used for OCI/Azure, and shapes the output to match
    what OciBox already renders (total / report / warnings) so the
    frontend needs no new component logic.
    """
    reqs = excel_to_requirements(file_bytes)

    if not reqs:
        return {
            "total": 0,
            "report": [],
            "warnings": ["No requirements found in the uploaded sheet"],
        }

    results = price_all_requirements(reqs)

    report = []
    warnings = []
    grand_total = 0.0

    for r in results:
        cost = r.get("monthly_cost_usd")
        if cost is not None:
            grand_total += cost

        metric_bits = []
        if r.get("quantity") is not None:
            metric_bits.append(f"{r['quantity']} {r.get('quantity_unit') or ''}".strip())
        metric = ", ".join(metric_bits)

        product = r.get("product")
        product_label = product.get("description") if isinstance(product, dict) else r.get("aws_service_code")

        report.append({
            "product": product_label or r.get("aws_service_code") or "Unknown",
            "category": r.get("category"),
            "metric": metric,
            "cost": cost,
            "calculation": r.get("calculation"),
        })

        if r.get("warning"):
            warnings.append(f"{r.get('category')}: {r['warning']}")

    return {
        "total": round(grand_total, 2),
        "report": report,
        "warnings": warnings,
    }