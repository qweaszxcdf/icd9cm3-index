import searchDataset from "../public/data/dataset.json";
import tabularDataset from "../public/data/tabular.json";
import pdfManifestData from "../public/data/pdf-manifest.json";

const DATA_COLUMNS = ["page", "level", "chinese", "english", "code"];
const TABULAR_PAGE_MIN = 21;
const TABULAR_PAGE_MAX = 415;
const TABULAR_PDF_KEY = "target.pdf";
const API_CACHE_SECONDS = 365 * 24 * 60 * 60;
const PDF_CACHE_SECONDS = 365 * 24 * 60 * 60;

const ROW_PAGE = 0;
const ROW_LEVEL = 1;
const ROW_CHINESE = 2;
const ROW_ENGLISH = 3;
const ROW_CODE = 4;
const ROW_SOURCE_FILE = 5;
const ROW_SEARCH_BLOB = 6;
const ROW_CODE_NORM = 7;
const ROW_PARENT = 8;
const ROW_SUBTREE_END = 9;

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

function rowToJson(dataset, index, matched = false) {
  const row = dataset.rows[index];
  const chinese = normalizeText(row[ROW_CHINESE]);
  const english = normalizeText(row[ROW_ENGLISH]);
  return {
    id: `r${index}`,
    page: row[ROW_PAGE],
    level: row[ROW_LEVEL],
    code: normalizeText(row[ROW_CODE]),
    chinese,
    english,
    matched,
    has_children: row[ROW_SUBTREE_END] > index + 1,
  };
}

function buildHierarchy(rows) {
  const tree = [];
  const stack = [];

  for (const node of rows) {
    node.children = [];

    while (stack.length && stack[stack.length - 1].level >= node.level) {
      stack.pop();
    }

    if (stack.length) {
      stack[stack.length - 1].children.push(node);
    } else {
      tree.push(node);
    }

    stack.push(node);
  }

  return tree;
}

function collectRelevantRows(dataset, resultIndices, markMatches = true) {
  const matchedIndices = new Set(resultIndices);
  if (!matchedIndices.size) return [];

  const includedIndices = new Set();
  for (const resultIndex of matchedIndices) {
    let index = resultIndex;
    while (index >= 0) {
      if (dataset.rows[index][ROW_LEVEL] > 0) includedIndices.add(index);
      index = dataset.rows[index][ROW_PARENT];
    }
  }

  return [...includedIndices]
    .sort((left, right) => left - right)
    .map((index) => rowToJson(dataset, index, markMatches && matchedIndices.has(index)));
}

function rowMatchesTarget(row, target, strictPrefix = false) {
  const targetText = normalizeText(target).toLowerCase();
  const english = normalizeText(row[ROW_ENGLISH]).toLowerCase();
  if (strictPrefix) {
    if (english === targetText) return true;
    if (!english.startsWith(targetText)) return false;
    const remainder = english.slice(targetText.length);
    return Boolean(remainder && " ,-/()—".includes(remainder[0]));
  }
  return english === targetText;
}

function findHierarchicalTargetRows(dataset, parts) {
  if (!parts.length) return [];

  const rows = dataset.rows;
  const candidates = [];
  for (let index = 0; index < rows.length; index += 1) {
    if (!rowMatchesTarget(rows[index], parts[0], true)) continue;

    let currentIndex = index;
    let success = true;
    for (const part of parts.slice(1)) {
      const startLevel = parseIntSafe(rows[currentIndex][ROW_LEVEL], 0);
      let foundIndex = -1;

      // Prefer a direct child. Without this pass, a matching descendant
      // under an earlier sibling (for example spermatic -> vein) can steal
      // a path that should resolve to the current node's direct child.
      for (let cursor = currentIndex + 1; cursor < rows.length; cursor += 1) {
        const nextLevel = parseIntSafe(rows[cursor][ROW_LEVEL], 0);
        if (nextLevel <= startLevel) break;
        if (rows[cursor][ROW_PARENT] !== currentIndex) continue;
        if (rowMatchesTarget(rows[cursor], part, true)) {
          foundIndex = cursor;
          break;
        }
      }

      // If there is no direct child, allow a deeper descendant for paths
      // such as Repair -> hernia NEC -> inguinal (unilateral).
      if (foundIndex === -1) {
        for (let cursor = currentIndex + 1; cursor < rows.length; cursor += 1) {
          const nextLevel = parseIntSafe(rows[cursor][ROW_LEVEL], 0);
          if (nextLevel <= startLevel) break;
          if (rowMatchesTarget(rows[cursor], part, true)) {
            foundIndex = cursor;
            break;
          }
        }
      }
      if (foundIndex === -1) {
        if (!rowMatchesTarget(rows[currentIndex], part, true)) {
          success = false;
          break;
        }
      } else {
        currentIndex = foundIndex;
      }
    }
    if (success) {
      // The same textual path can occur in multiple index sections. Prefer
      // the shallowest matching parent, which is the canonical category
      // entry rather than a deeper cross-reference section.
      candidates.push({
        index: currentIndex,
        firstExact: rowMatchesTarget(rows[index], parts[0]),
        parentLevel: parseIntSafe(rows[index][ROW_LEVEL], 0),
      });
    }
  }
  candidates.sort(
    (left, right) => left.parentLevel - right.parentLevel
      || Number(right.firstExact) - Number(left.firstExact)
      || left.index - right.index,
  );
  return candidates.length ? [candidates[0].index] : [];
}

function findOrderedTargetRows(dataset, parts) {
  if (!parts.length) return [];
  const rows = dataset.rows;
  const firstPart = parts[0].toLowerCase();
  for (let index = 0; index < rows.length; index += 1) {
    if (!rowMatchesTarget(rows[index], firstPart, true)) continue;
    let currentIndex = index;
    let partIndex = 1;
    for (let cursor = index + 1; cursor < rows.length && partIndex < parts.length; cursor += 1) {
      if (rowMatchesTarget(rows[cursor], parts[partIndex], true)) {
        currentIndex = cursor;
        partIndex += 1;
      }
    }
    if (partIndex === parts.length) return [currentIndex];
  }
  return [];
}

function getLocatePathVariants(parts) {
  const variants = [parts];
  if (parts.length > 1) {
    const qualifier = parts[parts.length - 1].trim().toLowerCase();
    if (qualifier === "by site" || qualifier === "按部位") {
      variants.push(parts.slice(0, -1));
    }
  }
  return variants;
}

function rowMatchesFilters(dataset, row, pageMin, pageMax, levelMin, levelMax, fileFilters) {
  if (pageMin !== null && row[ROW_PAGE] < pageMin) return false;
  if (pageMax !== null && row[ROW_PAGE] > pageMax) return false;
  if (levelMin !== null && row[ROW_LEVEL] < levelMin) return false;
  if (levelMax !== null && row[ROW_LEVEL] > levelMax) return false;
  if (fileFilters.length && !fileFilters.includes(dataset.source_files[row[ROW_SOURCE_FILE]])) return false;
  return true;
}

function rowSearchText(row, fields) {
  if (fields.length === DATA_COLUMNS.length && fields.every((field, index) => field === DATA_COLUMNS[index])) {
    return row[ROW_SEARCH_BLOB];
  }

  const values = [];
  for (const field of fields) {
    if (field === "page") values.push(String(row[ROW_PAGE]));
    else if (field === "level") values.push(String(row[ROW_LEVEL]));
    else if (field === "chinese") values.push(normalizeText(row[ROW_CHINESE]).toLowerCase());
    else if (field === "english") values.push(normalizeText(row[ROW_ENGLISH]).toLowerCase());
    else if (field === "code") values.push(row[ROW_CODE_NORM]);
  }
  return values.join(" ");
}

function rowMatchesSearch(row, queryLower, mode, fields, isCodeQuery = false) {
  if (!queryLower) return false;
  if (isCodeQuery || mode === "code") return row[ROW_CODE_NORM].startsWith(normalizeCode(queryLower));

  const text = rowSearchText(row, fields);
  if (mode === "any") {
    const tokens = queryLower.split(/\s+/).filter(Boolean);
    return tokens.some((token) => text.includes(token));
  }
  return text.includes(queryLower);
}

function searchRows(dataset, query, mode, fields, filters) {
  const rows = dataset.rows;
  const queryText = query.trim();
  if (!queryText) {
    const rootIndices = [];
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      if (row[ROW_LEVEL] === 0 && rowMatchesFilters(dataset, row, ...filters)) rootIndices.push(index);
    }
    return {
      count: rootIndices.length,
      limited: false,
      shown: rootIndices.length,
      treeRows: rootIndices.map((index) => rowToJson(dataset, index)),
    };
  }

  const isCodeQuery = /^\d+(?:\.\d+)?$/.test(queryText);
  const queryLower = queryText.toLowerCase();
  const resultIndices = [];
  let count = 0;
  const hasFilters = filters.some((value, index) => index < 4 ? value !== null : value.length > 0);

  if (isCodeQuery || mode === "code") {
    const codeNorm = normalizeCode(queryLower);
    const exactIndices = dataset.code_index[codeNorm] || [];
    if (exactIndices.length) {
      for (const index of exactIndices) {
        if (hasFilters && !rowMatchesFilters(dataset, rows[index], ...filters)) continue;
        count += 1;
        resultIndices.push(index);
      }
    } else {
      for (let index = 0; index < rows.length; index += 1) {
        const row = rows[index];
        if (!row[ROW_CODE_NORM].startsWith(codeNorm) || !rowMatchesFilters(dataset, row, ...filters)) continue;
        count += 1;
        resultIndices.push(index);
      }
    }
  } else {
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      if (!rowMatchesFilters(dataset, row, ...filters) || !rowMatchesSearch(row, queryLower, mode, fields)) continue;
      count += 1;
      resultIndices.push(index);
    }
  }

  return {
    count,
    limited: false,
    shown: resultIndices.length,
    treeRows: collectRelevantRows(dataset, resultIndices),
  };
}

function getDataset() {
  return searchDataset;
}

function getTabularData() {
  return tabularDataset;
}

function getPdfManifest() {
  return pdfManifestData;
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

async function handleSearch(request, env) {
  const url = new URL(request.url);
  const query = url.searchParams.get("q") || "";
  const mode = url.searchParams.get("mode") || "text";
  const fieldList = url.searchParams.get("fields") || "chinese,english,code";
  let fields = fieldList.split(",").filter((field) => DATA_COLUMNS.includes(field));
  if (!fields.length) fields = ["chinese", "english", "code"];

  const pageMin = url.searchParams.has("page_min") ? parseIntSafe(url.searchParams.get("page_min"), null) : null;
  const pageMax = url.searchParams.has("page_max") ? parseIntSafe(url.searchParams.get("page_max"), null) : null;
  const levelMin = url.searchParams.has("level_min") ? parseIntSafe(url.searchParams.get("level_min"), null) : null;
  const levelMax = url.searchParams.has("level_max") ? parseIntSafe(url.searchParams.get("level_max"), null) : null;
  const fileFilters = url.searchParams.getAll("file");

  const dataset = getDataset();
  const { count, limited, shown, treeRows } = searchRows(dataset, query, mode, fields, [
    pageMin,
    pageMax,
    levelMin,
    levelMax,
    fileFilters,
  ]);

  return jsonResponse({
    query,
    count,
    limited,
    shown,
    tree: buildHierarchy(treeRows),
  });
}

async function handleLocate(request, env) {
  const url = new URL(request.url);
  const target = (url.searchParams.get("target") || "").trim();
  if (!target) {
    return jsonResponse({ query: target, count: 0, rows: [], tree: [] });
  }

  const markParam = (url.searchParams.get("mark") || "true").toLowerCase();
  const mark = !(markParam === "0" || markParam === "false" || markParam === "no");
  const lowerTarget = target.toLowerCase();
  if (target.includes("亚目") || lowerTarget.includes("subcategory")) {
    return jsonResponse({ query: target, count: 0, rows: [], tree: [], ignored: true });
  }

  const dataset = getDataset();
  const rows = dataset.rows;
  let resultIndices = [];
  let count = 0;

  if (/^\d+(?:\.\d+)?$/.test(lowerTarget)) {
    const exactIndices = dataset.code_index[normalizeCode(lowerTarget)] || [];
    count = exactIndices.length;
    resultIndices = exactIndices;
  } else {
    const textCandidates = [];
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      const english = normalizeText(row[ROW_ENGLISH]).toLowerCase();
      if (english === lowerTarget || rowMatchesTarget(row, lowerTarget, true)) {
        textCandidates.push({
          index,
          exact: english === lowerTarget,
          level: parseIntSafe(row[ROW_LEVEL], 0),
        });
      }
    }

    if (textCandidates.length) {
      textCandidates.sort(
        (left, right) => left.level - right.level
          || Number(right.exact) - Number(left.exact)
          || left.index - right.index,
      );
      count = 1;
      resultIndices = [textCandidates[0].index];
    } else if (lowerTarget.includes(",") || lowerTarget.includes("，")) {
      const parts = lowerTarget.split(/[，,]/).map((part) => part.trim()).filter(Boolean);
      if (parts.length > 1) {
        for (const pathVariant of getLocatePathVariants(parts)) {
          resultIndices = findHierarchicalTargetRows(dataset, pathVariant);
          if (!resultIndices.length) resultIndices = findOrderedTargetRows(dataset, pathVariant);
          if (resultIndices.length) break;
        }
        count = resultIndices.length;
      }
    } else {
      const codeMatch = lowerTarget.match(/(\d+(?:\.\d+)?)$/);
      if (codeMatch) {
        const exactIndices = dataset.code_index[normalizeCode(codeMatch[1])] || [];
        count = exactIndices.length;
        resultIndices = exactIndices;
      }
    }
  }

  const resultRows = resultIndices.map((index) => rowToJson(dataset, index, mark));
  const treeRows = collectRelevantRows(dataset, resultIndices, mark);

  return jsonResponse({
    query: target,
    count,
    limited: false,
    shown: resultIndices.length,
    rows: resultRows,
    tree: buildHierarchy(treeRows),
  });
}

async function handleChildren(request, env) {
  const url = new URL(request.url);
  const nodeId = url.searchParams.get("id") || "";
  const query = url.searchParams.get("q") || "";
  const mode = url.searchParams.get("mode") || "auto";
  const fieldList = url.searchParams.get("fields") || "chinese,english,code";
  let fields = fieldList.split(",").filter((field) => DATA_COLUMNS.includes(field));
  if (!fields.length) fields = ["chinese", "english", "code"];

  const dataset = getDataset();
  const rows = dataset.rows;
  const idMatch = nodeId.match(/^r(\d+)$/);
  const startIndex = idMatch ? Number.parseInt(idMatch[1], 10) : -1;
  if (startIndex < 0 || startIndex >= rows.length) {
    return jsonResponse({ children: [] });
  }

  const children = [];
  const queryLower = query.trim().toLowerCase();
  const isCodeQuery = /^\d+(?:\.\d+)?$/.test(queryLower);
  const subtreeEnd = rows[startIndex][ROW_SUBTREE_END];

  for (let cursor = startIndex + 1; cursor < subtreeEnd; cursor += 1) {
    const row = rows[cursor];
    if (row[ROW_PARENT] !== startIndex) continue;
    const matched = rowMatchesSearch(row, queryLower, mode, fields, isCodeQuery);
    children.push(rowToJson(dataset, cursor, matched));
  }

  return jsonResponse({ children });
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

  const pdfManifest = getPdfManifest();
  const pdfKey = normalizeText(pdfManifest.key) || TABULAR_PDF_KEY;
  const totalSize = parseIntSafe(pdfManifest.total_size, 0);
  if (!pdfManifest.available || totalSize <= 0) {
    return new Response("Tabular PDF not found", { status: 404 });
  }

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
    if (url.pathname === "/api/search") return cachedJsonResponse(request, API_CACHE_SECONDS, () => handleSearch(request, env));
    if (url.pathname === "/api/locate") return cachedJsonResponse(request, API_CACHE_SECONDS, () => handleLocate(request, env));
    if (url.pathname === "/api/children") return cachedJsonResponse(request, API_CACHE_SECONDS, () => handleChildren(request, env));
    if (url.pathname === "/api/tabular") return cachedJsonResponse(request, API_CACHE_SECONDS, () => handleTabular(request, env));
    if (url.pathname === "/tabular-pdf") return handleTabularPdf(request, env);
    return env.ASSETS.fetch(request);
  },
};
