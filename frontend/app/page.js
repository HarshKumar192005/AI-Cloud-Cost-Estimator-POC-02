"use client";

import { useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function formatMoney(value, currency) {
  if (value === undefined || value === null || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency || "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function BoxHeader({ label, textClass }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-100 pb-3">
      <h2 className={`text-sm font-semibold uppercase tracking-wide ${textClass}`}>
        {label}
      </h2>
    </div>
  );
}

function AzureBox({ data }) {
  if (!data) return null;
  if (data.error) {
    return (
      <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-blue-600 bg-white p-5">
        <BoxHeader label="Azure" textClass="text-blue-700" />
        <p className="mt-4 text-sm text-red-600">{data.error}</p>
      </div>
    );
  }

  // Pull a billing-unit label out of the pricing object (e.g. "Price per GB",
  // "Hourly Price", "Cost per Million") so the right-hand column reads like
  // "Gigabyte Storage Capacity Per Month" without hardcoding it per service.
  const getUnitLabel = (pricing) => {
    if (!pricing) return "";
    const key = Object.keys(pricing).find(
      (k) => k !== "Price Source" && k !== "Source"
    );
    return key || "";
  };

  const getPriceSource = (pricing) =>
    pricing?.["Price Source"] || pricing?.["Source"] || "";

  return (
    <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-blue-600 bg-white p-5 flex flex-col">
      <BoxHeader label="Azure" textClass="text-blue-700" />

      <div className="mt-4">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Estimated monthly cost
        </p>
        <p className="text-3xl font-semibold tracking-tight text-slate-900">
          {formatMoney(data.total_monthly_cost, data.currency)}
        </p>
        {data.summary && (
          <p className="mt-1 text-xs text-slate-400">
            {data.summary.calculation_basis} · {data.summary.billing_period}{" "}
            billing · {data.summary.monthly_hours_assumed} hrs/month assumed
          </p>
        )}
      </div>

      {data.warnings?.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-amber-700">
          {data.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}

      <div className="mt-5 flex-1 overflow-auto rounded-md bg-slate-50 px-4">
        {data.breakdown?.length ? (
          data.breakdown.map((row, i) => (
            <div
              key={i}
              className="border-b border-slate-200 py-4 last:border-0"
            >
              {/* Top line: service + unit + price */}
              <div className="flex items-start justify-between gap-4">
                <p className="min-w-0 flex-1 font-semibold text-slate-900">
                  {row.service}
                  {row.sku ? ` (${row.sku})` : ""}
                </p>

                <p className="w-40 shrink-0 text-right text-xs text-slate-500">
                  {getUnitLabel(row.pricing)}
                </p>

                <p className="w-20 shrink-0 text-right font-semibold text-slate-900">
                  {formatMoney(row.monthly_cost, data.currency)}
                </p>
              </div>

              {/* Price source + calculation, kept where they were */}
              {getPriceSource(row.pricing) && (
                <p className="mt-0.5 text-xs text-slate-400">
                  {getPriceSource(row.pricing)}
                </p>
              )}
              {row.calculation && (
                <p className="mt-0.5 text-xs text-slate-400/80">
                  {row.calculation}
                </p>
              )}

              {/* Inputs — every key/value the API sent for this line item */}
              {row.inputs && Object.keys(row.inputs).length > 0 && (
                <p className="mt-2 text-xs text-slate-500">
                  {Object.entries(row.inputs)
                    .map(([k, v]) => `${k}: ${v}`)
                    .join(" · ")}
                </p>
              )}

              {/* Full pricing breakdown (covers keys beyond the unit label,
                  e.g. "Monthly Hours", "Base Price", "Data Charge/TB") */}
              {row.pricing && Object.keys(row.pricing).length > 0 && (
                <p className="mt-1 text-xs text-slate-500">
                  {Object.entries(row.pricing)
                    .filter(([k]) => k !== "Price Source" && k !== "Source")
                    .map(([k, v]) => `${k}: ${v}`)
                    .join(" · ")}
                </p>
              )}

              {/* Formula */}
              {row.formula && (
                <p className="mt-2 text-xs text-slate-500">
                  <span className="font-medium text-slate-600">Formula: </span>
                  {row.formula}
                </p>
              )}

              {/* Explanation */}
              {row.explanation && (
                <p className="mt-1 text-xs leading-relaxed text-slate-400">
                  {row.explanation}
                </p>
              )}
            </div>
          ))
        ) : (
          <p className="px-3 py-4 text-center text-slate-400">No line items</p>
        )}
      </div>
    </div>
  );
}

function OciBox({ data }) {
  if (!data) return null;

  if (data.error) {
    return (
      <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-rose-700 bg-white p-5">
        <BoxHeader label="OCI" textClass="text-rose-700" />
        <p className="mt-4 text-sm text-red-600">{data.error}</p>
      </div>
    );
  }

  return (
    <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-rose-700 bg-white p-5 flex flex-col">
      <BoxHeader label="OCI" textClass="text-rose-700" />

      <div className="mt-4">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Estimated monthly cost
        </p>
        <p className="text-3xl font-semibold tracking-tight text-slate-900">
          {formatMoney(data.total, "USD")}
        </p>
      </div>

      {data.warnings?.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-amber-700">
          {data.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}

      <div className="mt-5 flex-1 overflow-auto rounded-md bg-slate-50">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-slate-500">
              <th className="px-3 py-2 font-medium">Product</th>
              <th className="px-3 py-2 font-medium">Metric</th>
              <th className="px-3 py-2 font-medium text-right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.report?.length ? (
              data.report.map((row, i) => (
                <tr key={i} className="border-b border-slate-200 last:border-0 align-top">
                  <td className="px-3 py-2">
                    <div className="font-medium">{row.product}</div>
                    <div className="text-xs text-slate-500">{row.category}</div>
                    {row.calculation && (
                      <div className="mt-1 text-xs text-slate-400">
                        {row.calculation}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-500">{row.metric}</td>
                  <td className="px-3 py-2 text-right font-medium">
                    {formatMoney(row.cost, "USD")}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={3} className="px-3 py-4 text-center text-slate-400">
                  No line items
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AwsBox({ data }) {
  if (!data) return null;

  if (data.error) {
    return (
      <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-amber-500 bg-white p-5">
        <BoxHeader label="AWS" textClass="text-amber-600" />
        <p className="mt-4 text-sm text-red-600">{data.error}</p>
      </div>
    );
  }

  return (
    <div className="flex-1 rounded-lg border border-slate-200 border-t-4 border-t-amber-500 bg-white p-5 flex flex-col">
      <BoxHeader label="AWS" textClass="text-amber-600" />

      <div className="mt-4">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Estimated monthly cost
        </p>
        <p className="text-3xl font-semibold tracking-tight text-slate-900">
          {formatMoney(data.total, "USD")}
        </p>
      </div>

      {data.warnings?.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-amber-700">
          {data.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}

      <div className="mt-5 flex-1 overflow-auto rounded-md bg-slate-50">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-slate-500">
              <th className="px-3 py-2 font-medium">Product</th>
              <th className="px-3 py-2 font-medium">Metric</th>
              <th className="px-3 py-2 font-medium text-right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.report?.length ? (
              data.report.map((row, i) => (
                <tr key={i} className="border-b border-slate-200 last:border-0 align-top">
                  <td className="px-3 py-2">
                    <div className="font-medium">{row.product}</div>
                    <div className="text-xs text-slate-500">{row.category}</div>
                    {row.calculation && (
                      <div className="mt-1 text-xs text-slate-400">
                        {row.calculation}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-500">{row.metric}</td>
                  <td className="px-3 py-2 text-right font-medium">
                    {formatMoney(row.cost, "USD")}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={3} className="px-3 py-4 text-center text-slate-400">
                  No line items
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Home() {
  const [fileName, setFileName] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const inputRef = useRef(null);

  const runEstimate = async (selectedFile) => {
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const formData = new FormData();
      formData.append("file", selectedFile);

      const res = await fetch(`${API_URL}/api/estimate?cloud=all`, {
        method: "POST",
        body: formData,
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || "Estimate request failed");
      }

      setResult(data);
    } catch (err) {
      setError(err.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  };

  const handleFileChange = (e) => {
    const selected = e.target.files?.[0];
    if (!selected) return;
    setFileName(selected.name);
    runEstimate(selected);
    // allow re-selecting the same file later to re-run the estimate
    e.target.value = "";
  };

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="mb-8 flex flex-col gap-4 border-b border-slate-200 pb-6 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
            Cloud Cost Estimator
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Upload a requirements sheet to compare Azure, OCI, and AWS monthly estimates side by side.
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-3">
          {fileName && (
            <span className="max-w-[180px] truncate text-xs text-slate-400" title={fileName}>
              {fileName}
            </span>
          )}
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={loading}
            className="whitespace-nowrap rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            {loading ? "Estimating…" : "Upload sheet"}
          </button>
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx,.xlsm"
            onChange={handleFileChange}
            disabled={loading}
            className="hidden"
          />
        </div>
      </header>

      {error && (
        <div className="mb-6 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {loading && (
        <div className="mb-6 flex items-center gap-3 rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-500">
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />
          Calculating your estimate…
        </div>
      )}

      {result && !loading && (
        <div className="flex flex-col gap-6 md:flex-row">
          <AzureBox data={result.azure} />
          <OciBox data={result.oci} />
          <AwsBox data={result.aws} />
        </div>
      )}

      {!result && !error && !loading && (
        <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center text-slate-400">
          Upload a requirements sheet to see the Azure, OCI, and AWS estimates here.
        </div>
      )}
    </main>
  );
}