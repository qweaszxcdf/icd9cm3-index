import { handleSearch, handleLocate, handleChildren } from "../../static/index-core.mjs";
import searchDataset from "../public/data/dataset.json";
import tabularDataset from "../public/data/tabular.json";

const TABULAR_PAGE_MIN = 21;
const TABULAR_PAGE_MAX = 415;
const TABULAR_PDF_KEY = "target.pdf";
const API_CACHE_SECONDS = 365 * 24 * 60 * 60;
const PDF_CACHE_SECONDS = 365 * 24 * 60 * 60;

function normalizeText(value) {
  if (value === null || value === undefined) return "";
  const text = String(value);
  if (text.toLowerCase() === "nan") return "";
  return text.trim();
}

function normalizeCode(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim().replace(/\s+/g, "").toLowerCase();
}

function parseIntSafe(value, defaultValue = 0) {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  return Number.isNaN(parsed) ? defaultValue : parsed;
}

function parseCodeKey(code) {
  if (!code) return null;
  const text = normalizeCode(code);
  if (!text || !/^\d{1,2}(?:\.\d{1,2})?$/.test(text)) return null;
  if (!text.includes(".")) return [Number.parseInt(text, 10), -1];

  const [majorText, minorText] = text.split(".", 2);
  const minorScaled = Number.parseInt(minorText.padEnd(2, "0").slice(0, 2), 10);
  return [Number.parseInt(majorText, 10), minorScaled];
}

function compareCodeKeys(left, right) {
  if (left[0] !== right[0]) return left[0] - right[0];
  return left[1] - right[1];
}

function compareText(left, right) {
  const leftText = normalizeText(left);
  const rightText = normalizeText(right);
  if (leftText < rightText) return -1;
  if (leftText > rightText) return 1;
  return 0;
}

function hasPdfBucket(env) {
  return Boolean(env && env.TABULAR_PDF);
}

function getTabularData() {
  return tabularDataset;
}

function jsonResponse(payload, init = {}) {
  return new Response(JSON.stringify(payload), {
    ...init,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
  });
}

function addCacheHeaders(response, seconds, extra = {}) {
  const cached = new Response(response.body, response);
  cached.headers.set("cache-control", `public, max-age=${seconds}, s-maxage=${seconds}`);
  cached.headers.set("cdn-cache-control", `public, max-age=${seconds}`);
  cached.headers.set("cloudflare-cdn-cache-control", `public, max-age=${seconds}`);
  for (const [key, value] of Object.entries(extra)) {
    cached.headers.set(key, value);
  }
  return cached;
}

async function cachedJsonResponse(request, seconds, createResponse) {
  const response = await createResponse();
  if (request.method !== "GET" || !response.ok) return response;
  return addCacheHeaders(response, seconds);
}

async function handleTabular(request, env) {
  const url = new URL(request.url);
  const queryCode = (url.searchParams.get("code") || "").trim();
  if (!queryCode) {
    return jsonResponse({ query: queryCode, count: 0, rows: [], page: null });
  }

  const codeNorm = normalizeCode(queryCode);
  if (!codeNorm) {
    return jsonResponse({ query: queryCode, count: 0, rows: [], page: null });
  }

  const tabularData = getTabularData();
  const tabularRows = tabularData.rows || [];
  const pdfAvailable = hasPdfBucket(env);
  let rows = [];

  const exactMatches = tabularRows.filter((row) => row.code_norm === codeNorm);
  if (exactMatches.length) {
    rows = [...exactMatches].sort((left, right) => {
      const pageDelta = parseIntSafe(left.page, -1) - parseIntSafe(right.page, -1);
      if (pageDelta !== 0) return pageDelta;
      const codeDelta = compareText(normalizeCode(left.code), normalizeCode(right.code));
      if (codeDelta !== 0) return codeDelta;
      return compareText(left.row_type, right.row_type);
    }).slice(0, 10);
  } else {
    const queryKey = parseCodeKey(codeNorm);
    let selectedBoundary = null;
    if (queryKey) {
      const seenPages = new Set();
      const boundaries = [...tabularRows]
        .sort((left, right) => {
          const pageDelta = parseIntSafe(left.page, -1) - parseIntSafe(right.page, -1);
          if (pageDelta !== 0) return pageDelta;
          return compareText(normalizeCode(left.code), normalizeCode(right.code));
        })
        .filter((row) => {
          const page = parseIntSafe(row.page, -1);
          const boundaryKey = parseCodeKey(row.code_norm);
          if (seenPages.has(page) || !boundaryKey) return false;
          seenPages.add(page);
          row.code_key = boundaryKey;
          return true;
        });

      for (const boundary of boundaries) {
        const boundaryKey = boundary.code_key;
        if (!boundaryKey) continue;
        if (compareCodeKeys(boundaryKey, queryKey) > 0) continue;
        if (selectedBoundary === null) {
          selectedBoundary = boundary;
          continue;
        }
        const comparison = compareCodeKeys(boundaryKey, selectedBoundary.code_key);
        if (comparison > 0 || (comparison === 0 && parseIntSafe(boundary.page, -1) < parseIntSafe(selectedBoundary.page, -1))) {
          selectedBoundary = boundary;
        }
      }
    }

    if (!selectedBoundary) {
      return jsonResponse({ query: queryCode, count: 0, rows: [], page: null });
    }

    rows = [{ page: selectedBoundary.page, code: selectedBoundary.code, row_type: "page_boundary" }];
  }

  const selected = rows[0];
  const page = parseIntSafe(selected.page, -1);
  return jsonResponse({
    query: queryCode,
    count: rows.length,
    rows: rows.map((row) => ({
      page: parseIntSafe(row.page, -1),
      code: normalizeText(row.code),
      row_type: normalizeText(row.row_type),
    })),
    page,
    pdf_url: pdfAvailable ? "/tabular-pdf" : "",
  });
}

async function handleTabularPdf(request, env) {
  if (!hasPdfBucket(env)) {
    return new Response("Tabular PDF not found", { status: 404 });
  }

  // R2 is the source of truth: a build without a local target.pdf must not disable the PDF.
  const pdfKey = TABULAR_PDF_KEY;
  const metadata = await env.TABULAR_PDF.head(pdfKey);
  if (!metadata || metadata.size <= 0) {
    return new Response("Tabular PDF not found", { status: 404 });
  }
  const totalSize = metadata.size;

  const rangeHeader = request.headers.get("range") || "";
  const parsedRange = parseRangeHeader(rangeHeader, totalSize);
  if (parsedRange && !parsedRange.valid) {
    return rangeNotSatisfiable(totalSize);
  }

  const object = parsedRange
    ? await env.TABULAR_PDF.get(pdfKey, {
        range: {
          offset: parsedRange.start,
          length: parsedRange.end - parsedRange.start + 1,
        },
      })
    : await env.TABULAR_PDF.get(pdfKey);

  if (!object || !object.body) {
    return new Response("Tabular PDF not found", { status: 404 });
  }

  const headers = new Headers({
    "content-type": "application/pdf",
    "accept-ranges": "bytes",
    "cache-control": `public, max-age=${PDF_CACHE_SECONDS}, s-maxage=${PDF_CACHE_SECONDS}`,
    "cdn-cache-control": `public, max-age=${PDF_CACHE_SECONDS}`,
    "cloudflare-cdn-cache-control": `public, max-age=${PDF_CACHE_SECONDS}`,
  });
  if (object.httpEtag) {
    headers.set("etag", object.httpEtag);
  }

  if (parsedRange) {
    headers.set("content-range", `bytes ${parsedRange.start}-${parsedRange.end}/${totalSize}`);
    headers.set("content-length", String(parsedRange.end - parsedRange.start + 1));
    return new Response(object.body, { status: 206, headers });
  }

  headers.set("content-length", String(totalSize));
  return new Response(object.body, { headers });
}

function parseRangeHeader(rangeHeader, totalSize) {
  if (!rangeHeader) return null;
  if (!rangeHeader.startsWith("bytes=")) return { valid: false };

  const rangeSpec = rangeHeader.slice(6).split(",", 1)[0].trim();
  const [startText = "", endText = ""] = rangeSpec.split("-", 2);
  const parsedStart = startText ? Number.parseInt(startText, 10) : NaN;
  const parsedEnd = endText ? Number.parseInt(endText, 10) : NaN;

  if (!startText && !endText) return { valid: false };

  let start;
  let end;
  if (!startText) {
    if (Number.isNaN(parsedEnd) || parsedEnd <= 0) return { valid: false };
    start = Math.max(0, totalSize - parsedEnd);
    end = totalSize - 1;
  } else if (!Number.isNaN(parsedStart)) {
    start = parsedStart;
    end = Number.isNaN(parsedEnd) ? totalSize - 1 : Math.min(parsedEnd, totalSize - 1);
  } else {
    return { valid: false };
  }

  if (start < 0 || start >= totalSize || end < start) return { valid: false };
  return { valid: true, start, end };
}

function rangeNotSatisfiable(totalSize) {
  return new Response("Requested Range Not Satisfiable", {
    status: 416,
    headers: {
      "content-range": `bytes */${totalSize}`,
      "accept-ranges": "bytes",
      "cache-control": "no-store",
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // Prevent cached search paths from resolving IDs against another build.
    if (url.pathname.startsWith("/api/")) {
      const revision = url.searchParams.get("v");
      if (revision && !revision.startsWith("legacy-") && revision !== searchDataset.meta.version) {
        return jsonResponse(
          { error: "dataset_version_mismatch", current_version: searchDataset.meta.version },
          { status: 409, headers: { "cache-control": "no-store" } },
        );
      }
    }
    if (url.pathname === "/api/search") return cachedJsonResponse(request, API_CACHE_SECONDS, () => jsonResponse(handleSearch(request, searchDataset)));
    if (url.pathname === "/api/locate") return cachedJsonResponse(request, API_CACHE_SECONDS, () => jsonResponse(handleLocate(request, searchDataset)));
    if (url.pathname === "/api/children") return cachedJsonResponse(request, API_CACHE_SECONDS, () => jsonResponse(handleChildren(request, searchDataset)));
    if (url.pathname === "/api/tabular") return cachedJsonResponse(request, API_CACHE_SECONDS, () => handleTabular(request, env));
    if (url.pathname === "/tabular-pdf") return handleTabularPdf(request, env);
    return env.ASSETS.fetch(request);
  },
};
