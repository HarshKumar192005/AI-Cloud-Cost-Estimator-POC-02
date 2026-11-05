import math
import re

import requests

AZURE_PRICING_API = "https://prices.azure.com/api/retail/prices"

REGION = "eastus"

MONTHLY_HOURS = 744

# Premium SSD tiers (name, size in GiB). These are product sizes, not prices.
DISK_TIERS = [
    ("P4", 32), ("P6", 64), ("P10", 128), ("P15", 256), ("P20", 512),
    ("P30", 1024), ("P40", 2048), ("P50", 4096), ("P60", 8192),
    ("P70", 16384), ("P80", 32767),
]


class AzurePricingCalculator:

    def __init__(self):
        self.total_cost = 0
        self.breakdown = []
        self.warnings = []
        self._cache = {}

    ###############################################################
    # Generic Azure Retail Pricing API
    ###############################################################

    def _fetch(self, filter_query):
        """Call the API (following pagination) and cache by filter."""

        if filter_query in self._cache:
            return self._cache[filter_query]

        items = []
        url = AZURE_PRICING_API
        params = {"$filter": filter_query}

        while url:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code != 200:
                break

            data = response.json()
            items.extend(data.get("Items", []))

            url = data.get("NextPageLink")
            params = None  # NextPageLink already contains the filter

        self._cache[filter_query] = items
        return items

    def get_price_item(
        self,
        service_name,
        arm_sku=None,
        product=None,
        sku=None,
        meter_contains=None,
        exclude=("spot", "low priority", "windows"),
    ):
        """Return the best matching price item (dict) or None."""

        filter_query = (
            f"serviceName eq '{service_name}'"
            f" and armRegionName eq '{REGION}'"
            f" and priceType eq 'Consumption'"
        )

        if arm_sku:
            filter_query += f" and armSkuName eq '{arm_sku}'"
        if product:
            filter_query += f" and productName eq '{product}'"
        if sku:
            filter_query += f" and skuName eq '{sku}'"

        candidates = []

        for item in self._fetch(filter_query):

            text = " ".join([
                item.get("productName", ""),
                item.get("skuName", ""),
                item.get("meterName", ""),
            ]).lower()

            if any(word in text for word in exclude):
                continue

            if meter_contains and meter_contains.lower() not in item.get("meterName", "").lower():
                continue

            if item.get("unitPrice", 0) <= 0:
                continue

            candidates.append(item)

        if not candidates:
            return None

        # Prefer the lowest pricing tier
        candidates.sort(key=lambda i: i.get("tierMinimumUnits", 0))
        return candidates[0]

    def get_price(self, *args, **kwargs):
        item = self.get_price_item(*args, **kwargs)
        return item["unitPrice"] if item else None

    @staticmethod
    def unit_divisor(unit_of_measure):
        """'10K' -> 10000, '1M' -> 1000000, '1 Hour' -> 1"""

        match = re.match(r"\s*([\d,\.]+)\s*([KkMm]?)", unit_of_measure or "")

        if not match:
            return 1

        number = float(match.group(1).replace(",", ""))
        suffix = match.group(2).upper()

        return number * {"": 1, "K": 1_000, "M": 1_000_000}[suffix]

    def search_prices(self, service_name, contains=""):
        """Debug helper: print meters so you can verify the filters below."""

        filter_query = (
            f"serviceName eq '{service_name}'"
            f" and armRegionName eq '{REGION}'"
            f" and priceType eq 'Consumption'"
        )

        for item in self._fetch(filter_query):
            line = " | ".join([
                item.get("productName", ""),
                item.get("skuName", ""),
                item.get("meterName", ""),
                item.get("unitOfMeasure", ""),
                str(item.get("unitPrice")),
            ])
            if contains.lower() in line.lower():
                print(line)

    def _record_missing(self, service, detail):
        """No price found: record it instead of inventing a number."""

        message = f"{service}: no price found in Azure Retail Pricing API ({detail})"
        self.warnings.append(message)

        self.breakdown.append({
            "service": service,
            "monthly_cost": 0,
            "explanation": message
        })

    ###############################################################
    # Compute
    ###############################################################

    def calculate_compute(self, vm):

        ocpu = vm["OCPU"]
        memory = vm["Memory_GB"]
        count = vm["Count"]

        if ocpu >= 32:
            sku = "Standard_D32s_v5"
        elif ocpu >= 16:
            sku = "Standard_D16s_v5"
        elif ocpu >= 8:
            sku = "Standard_D8s_v5"
        else:
            sku = "Standard_D4s_v5"

        price_per_hour = self.get_price(
            "Virtual Machines",
            arm_sku=sku
        )

        if price_per_hour is None:
            self._record_missing("Azure Virtual Machine", sku)
            return

        monthly_hours = MONTHLY_HOURS

        cost = price_per_hour * monthly_hours * count

        self.total_cost += cost

        self.breakdown.append({

            "service": "Azure Virtual Machine",

            "sku": sku,

            "inputs": {
                "OCPU": ocpu,
                "Memory (GB)": memory,
                "VM Count": count
            },

            "pricing": {
                "Price Source": "Azure Retail Pricing API",
                "Hourly Price": round(price_per_hour, 4),
                "Monthly Hours": monthly_hours
            },

            "formula":
                "Monthly Cost = Hourly Price × Monthly Hours × VM Count",

            "calculation":
                f"{price_per_hour:.4f} × {monthly_hours} × {count} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation": (
                f"Selected Azure SKU '{sku}' based on {ocpu} OCPU. "
                f"Azure charges Virtual Machines on an hourly basis. "
                f"The hourly price is multiplied by {monthly_hours} hours "
                f"and then multiplied by the number of VMs."
            )
        })

    ###############################################################
    # Managed Disk
    ###############################################################

    def calculate_block_storage(self, storage):

        gb = storage["Storage_GB"]

        # Managed disks are priced per disk tier per month, not per GB
        tier, tier_gb = next(
            ((n, s) for n, s in DISK_TIERS if s >= gb),
            DISK_TIERS[-1]
        )

        disk_count = math.ceil(gb / tier_gb)

        price = self.get_price(
            "Storage",
            product="Premium SSD Managed Disks",
            sku=f"{tier} LRS",
            meter_contains=f"{tier} LRS Disk"
        )

        if price is None:
            self._record_missing("Managed Disk", f"{tier} LRS")
            return

        cost = disk_count * price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Managed Disk",

            "inputs": {
                "Disk Size (GB)": gb,
                "Disk Tier": tier,
                "Disk Count": disk_count
            },

            "pricing": {
                "Price per Disk / Month": price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Monthly Cost = Disk Count × Price per Disk",

            "calculation":
                f"{disk_count} × {price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                f"Azure Managed Disks are billed per provisioned disk tier. "
                f"{gb} GB maps to {disk_count} × {tier} ({tier_gb} GiB)."
        })

    ###############################################################
    # Blob Storage
    ###############################################################

    def calculate_object_storage(self, obj):

        gb = obj["Storage_GB"]

        requests_count = obj["Requests_Per_Month"]

        storage_item = self.get_price_item(
            "Storage",
            product="General Block Blob v2",
            sku="Hot LRS",
            meter_contains="Data Stored"
        )

        request_item = self.get_price_item(
            "Storage",
            product="General Block Blob v2",
            sku="Hot LRS",
            meter_contains="Write Operations"
        )

        if storage_item is None or request_item is None:
            self._record_missing("Blob Storage", "Hot LRS storage/operations")
            return

        storage_price = storage_item["unitPrice"]

        # Operations are priced per 10K, so convert to price per request
        request_price = request_item["unitPrice"] / self.unit_divisor(
            request_item.get("unitOfMeasure")
        )

        storage_cost = gb * storage_price

        request_cost = requests_count * request_price

        cost = storage_cost + request_cost

        self.total_cost += cost

        self.breakdown.append({

            "service": "Blob Storage",

            "inputs": {
                "Storage (GB)": gb,
                "Requests": requests_count
            },

            "pricing": {
                "Storage Price/GB": storage_price,
                "Request Price": request_price,
                "Source": "Azure Retail Pricing API"
            },

            "formula":
                "(Storage × Price/GB) + (Requests × Request Price)",

            "calculation":
                f"({gb} × {storage_price}) + ({requests_count} × {request_price}) = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Blob Storage cost consists of storage capacity charges and transaction charges."
        })

    ###############################################################
    # Azure Files
    ###############################################################

    def calculate_file_storage(self, file):

        gb = file["Storage_GB"]

        price = self.get_price(
            "Storage",
            product="Premium Files",
            sku="Premium LRS",
            meter_contains="Provisioned"
        )

        if price is None:
            self._record_missing("Azure Files", "Premium LRS Provisioned")
            return

        cost = gb * price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Azure Files",

            "inputs": {
                "Storage (GB)": gb
            },

            "pricing": {
                "Price per GB": price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Storage × Price per GB",

            "calculation":
                f"{gb} × {price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure Files is billed according to the provisioned storage capacity."
        })

    ###############################################################
    # Load Balancer
    ###############################################################

    def calculate_load_balancer(self, lb):

        bandwidth = lb["Bandwidth_Mbps"]

        base_item = self.get_price_item(
            "Load Balancer",
            sku="Standard",
            meter_contains="Included LB Rules"
        )

        data_item = self.get_price_item(
            "Load Balancer",
            sku="Standard",
            meter_contains="Data Processed"
        )

        if base_item is None or data_item is None:
            self._record_missing("Standard Load Balancer", "Standard rules/data processed")
            return

        base_price = base_item["unitPrice"] * MONTHLY_HOURS  # hourly -> monthly

        data_processed_tb = bandwidth / 1024

        data_price_per_gb = data_item["unitPrice"]

        data_price = data_processed_tb * 1024 * data_price_per_gb

        cost = base_price + data_price

        self.total_cost += cost

        self.breakdown.append({

            "service": "Standard Load Balancer",

            "inputs": {
                "Bandwidth (Mbps)": bandwidth
            },

            "pricing": {
                "Base Price (Monthly)": round(base_price, 4),
                "Data Charge/GB": data_price_per_gb,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Base Price + (Bandwidth/1024 × 1024 × Data Charge per GB)",

            "calculation":
                f"{base_price:.4f} + ({bandwidth}/1024 × 1024 × {data_price_per_gb}) = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure Load Balancer pricing includes a fixed monthly charge plus data processing charges."
        })

    ###############################################################
    # ExpressRoute (FastConnect equivalent)
    ###############################################################

    def calculate_express_route(self, conn):

        hours = conn["Port_Hours"]

        hourly_price = self.get_price(
            "ExpressRoute",
            meter_contains="Port"
        )

        if hourly_price is None:
            self._record_missing("ExpressRoute", "Port hours")
            return

        cost = hours * hourly_price

        self.total_cost += cost

        self.breakdown.append({

            "service": "ExpressRoute",

            "inputs": {
                "Port Hours": hours
            },

            "pricing": {
                "Hourly Price": hourly_price,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "Port Hours × Hourly Price",

            "calculation":
                f"{hours} × {hourly_price} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "ExpressRoute is billed based on the active port hours."
        })

    ###############################################################
    # API Management
    ###############################################################

    def calculate_api_management(self, api):

        calls = api["API_Calls_Million"]

        item = self.get_price_item(
            "API Management",
            sku="Consumption",
            meter_contains="Calls"
        )

        if item is None:
            self._record_missing("API Management", "Consumption calls")
            return

        # Convert price to "per one million calls" using the API's unit
        cost_per_million = item["unitPrice"] * (
            1_000_000 / self.unit_divisor(item.get("unitOfMeasure"))
        )

        cost = calls * cost_per_million

        self.total_cost += cost

        self.breakdown.append({

            "service": "API Management",

            "inputs": {
                "API Calls (Million)": calls
            },

            "pricing": {
                "Cost per Million": cost_per_million,
                "Price Source": "Azure Retail Pricing API"
            },

            "formula":
                "API Calls × Cost per Million",

            "calculation":
                f"{calls} × {cost_per_million} = {cost:.2f}",

            "monthly_cost": round(cost, 2),

            "explanation":
                "Azure API Management pricing is calculated according to the number of API calls."
        })

    ###############################################################
    # Main Calculator
    ###############################################################

    def calculate(self, requirements):

        for service in requirements:

            category = service["category"]

            if category == "Compute":

                for vm in service["details"]:
                    self.calculate_compute(vm)

            elif category == "Storage":

                for item in service["details"]:

                    if item["Type"] == "Block":
                        self.calculate_block_storage(item)

                    elif item["Type"] == "Object":
                        self.calculate_object_storage(item)

                    elif item["Type"] == "File":
                        self.calculate_file_storage(item)

            elif category == "Flexible Load Balancer":

                for lb in service["details"]:
                    self.calculate_load_balancer(lb)

            elif category == "Networking - FastConnect":

                for fc in service["details"]:
                    self.calculate_express_route(fc)

            elif category == "Application Development - API Management":

                for api in service["details"]:
                    self.calculate_api_management(api)

        return {
            "cloud": "Azure",

            "currency": "USD",

            "total_monthly_cost": round(self.total_cost, 2),

            "summary": {
                "pricing_model": "Azure Retail Pricing API",
                "calculation_basis": "Pay-as-you-go",
                "billing_period": "Monthly",
                "monthly_hours_assumed": MONTHLY_HOURS,
                "formula": "Total Cost = Sum of all individual service monthly costs"
            },

            "warnings": self.warnings,

            "breakdown": self.breakdown
        }