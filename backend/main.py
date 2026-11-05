from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from azure_pricing import AzurePricingCalculator
from oci_pricing import excel_to_requirements, fetch_products, run_oci_estimate


from azure_pricing import AzurePricingCalculator
from oci_pricing import excel_to_requirements, fetch_products, run_oci_estimate
from aws_pricing import run_aws_estimate  # + new import

app = FastAPI(title="Cloud Cost Estimator (Azure + OCI)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your frontend's origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_azure_estimate(file_bytes: bytes) -> dict:
    """
    Runs the (unchanged) AzurePricingCalculator against the same
    'Category: ...' shaped Excel sheet used for OCI, so one uploaded
    file can drive both engines.
    """
    reqs = excel_to_requirements(file_bytes)

    if not reqs:
        return {
            "cloud": "Azure",
            "total_monthly_cost": 0,
            "currency": "USD",
            "breakdown": [],
            "warnings": ["No requirements found in the uploaded sheet"],
        }

    calculator = AzurePricingCalculator()
    result = calculator.calculate(reqs)
    result["warnings"] = []
    return result


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/refresh-products")
def refresh_products():
    """Refreshes the cached OCI product price list."""
    products = fetch_products(force=True)
    return {"count": len(products)}





@app.post("/api/estimate")
async def estimate(
    file: UploadFile = File(...),
    cloud: Optional[str] = Query(
        "all",
        description="Which engine(s) to run: 'azure', 'oci', 'aws', or 'all' (default).",
    ),
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an .xlsx file")

    cloud = (cloud or "all").lower().strip()
    valid = ("azure", "oci", "aws", "all", "both")  # 'both' kept for backward compat = azure+oci
    if cloud not in valid:
        raise HTTPException(400, "cloud must be one of: azure, oci, aws, all")

    file_bytes = await file.read()
    response: dict = {"cloud": cloud}

    if cloud in ("azure", "both", "all"):
        try:
            response["azure"] = run_azure_estimate(file_bytes)
        except Exception as e:
            response["azure"] = {"error": str(e)}

    if cloud in ("oci", "both", "all"):
        try:
            response["oci"] = run_oci_estimate(file_bytes)
        except Exception as e:
            response["oci"] = {"error": str(e)}

    if cloud in ("aws", "all"):
        try:
            response["aws"] = run_aws_estimate(file_bytes)
        except Exception as e:
            response["aws"] = {"error": str(e)}

    return response