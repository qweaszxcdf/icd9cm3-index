const DATASET_URL = "https://assets.local/data/dataset.json";
const PDF_MANIFEST_URL = "https://assets.local/data/pdf-manifest.json";
const DATA_COLUMNS = ["page", "level", "chinese", "english", "code"];
const TABULAR_PAGE_MIN = 21;
const TABULAR_PAGE_MAX = 415;
const TABULAR_PDF_KEY = "target.pdf";
const API_CACHE_SECONDS = 24 * 60 * 60;
const PDF_CACHE_SECONDS = 7 * 24 * 60 * 60;

let datasetPromise = null;
let pdfManifestPromise = null;

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

function extractReferences(text, language) {
  if (!text) return [];
  const refs = [];
  if (language === "zh") {
    const pattern = /(?:[-（(]?\s*)(另见|见)\s*([^；;\n]+)/gi;
    for (const match of text.matchAll(pattern)) {
      const target = match[2].trim();
      if (target) refs.push({ kind: match[1], target });
    }
  } else {
    const pattern = /\b(see also|see)\s+([^;\n]+)/gi;
    for (const match of text.matchAll(pattern)) {
      const target = match[2].trim();
      if (target) refs.push({ kind: match[1].toLowerCase(), target });
    }
  }
  return refs;
}

function getRowId(row) {
  return `${parseIntSafe(row.page, 0)}-${parseIntSafe(row.level, 0)}-${normalizeText(row._source_file)}-${parseIntSafe(row._source_line, 0)}`;
}

function hasPdfBucket(env) {
  return Boolean(env && env.TABULAR_PDF);
}

function rowToJson(row, matched = false, hasChildren = false) {
  const chinese = normalizeText(row.chinese);
  const english = normalizeText(row.english);
  return {
    id: getRowId(row),
    page: parseIntSafe(row.page, 0),
    level: parseIntSafe(row.level, 0),
    code: normalizeText(row.code),
    chinese,
    english,
    source_file: normalizeText(row._source_file),
    source_line: parseIntSafe(row._source_line, 0),
    references: extractReferences(chinese, "zh").concat(extractReferences(english, "en")),
    matched,
    has_children: hasChildren,
  };
}

function hasDescendants(index, allRows) {
  if (index + 1 >= allRows.length) return false;
  return parseIntSafe(allRows[index + 1].level, 0) > parseIntSafe(allRows[index].level, 0);
}

function hasDescendantsFromLevels(index, levels) {
  if (index + 1 >= levels.length) return false;
  return parseIntSafe(levels[index + 1], 0) > parseIntSafe(levels[index], 0);
}

function buildHierarchy(rows) {
  const ordered = [...rows].sort((left, right) => {
    const pageDelta = parseIntSafe(left.page, 0) - parseIntSafe(right.page, 0);
    if (pageDelta !== 0) return pageDelta;
    const sourceFileDelta = compareText(left.source_file, right.source_file);
    if (sourceFileDelta !== 0) return sourceFileDelta;
    return parseIntSafe(left.source_line, 0) - parseIntSafe(right.source_line, 0);
  });

  const tree = [];
  const stack = [];

  for (const node of ordered) {
    let level = parseIntSafe(node.level, 0);
    if (level < 0) level = 0;

    node.level = level;
    node.id ||= `${node.page ?? 0}-${level}-${node.source_file ?? ""}-${node.source_line ?? 0}`;
    node.page ||= 0;
    node.code ||= "";
    node.chinese ||= "";
    node.english ||= "";
    node.source_file ||= "";
    node.source_line ||= 0;
    if (!node.references) {
      node.references = extractReferences(normalizeText(node.chinese), "zh").concat(extractReferences(normalizeText(node.english), "en"));
    }
    node.has_children = Boolean(node.has_children);
    node.matched = Boolean(node.matched);
    node.children = [];

    while (stack.length && stack[stack.length - 1].level >= level) {
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

function uniqueRows(rows) {
  const seen = new Set();
  const unique = [];
  for (const row of rows) {
    const id = getRowId(row);
    if (seen.has(id)) continue;
    seen.add(id);
    unique.push(row);
  }
  return unique;
}

function collectRelevantRows(results, allRows) {
  const matchedIndices = new Set(results.map((row) => allRows.indexOf(row)).filter((index) => index >= 0));
  if (!matchedIndices.size) return [];

  const ancestorIndices = new Set();
  const levels = allRows.map((row) => row.level);

  for (const rowIndex of matchedIndices) {
    let currentLevel = parseIntSafe(levels[rowIndex], 0);
    let searchIndex = rowIndex;
    while (searchIndex > 0 && currentLevel > 0) {
      searchIndex -= 1;
      const priorLevel = parseIntSafe(levels[searchIndex], 0);
      if (priorLevel < currentLevel) {
        if (priorLevel > 0) {
          ancestorIndices.add(searchIndex);
        }
        currentLevel = priorLevel;
      }
    }
  }

  return [...matchedIndices, ...ancestorIndices]
    .sort((left, right) => left - right)
    .map((index) => rowToJson(allRows[index], matchedIndices.has(index), hasDescendantsFromLevels(index, levels)))
    .filter((row, index, array) => array.findIndex((candidate) => candidate.id === row.id) === index);
}

function rowMatchesTarget(row, targetLower, strictPrefix = false) {
  for (const key of ["english", "chinese"]) {
    const text = normalizeText(row[key]).toLowerCase();
    if (!text) continue;
    if (strictPrefix) {
      if (text === targetLower) return true;
      if (text.startsWith(targetLower)) {
        const remainder = text.slice(targetLower.length);
        if (remainder && [" ", ",", "-", "/", "(", ")", "—"].includes(remainder[0])) {
          return true;
        }
      }
    } else if (text === targetLower || text.startsWith(targetLower) || text.includes(targetLower)) {
      return true;
    }
  }
  return false;
}

function findOrderedTargetRows(rows, parts) {
  const scored = [];
  rows.forEach((row, index) => {
    const english = normalizeText(row.english).toLowerCase();
    const chinese = normalizeText(row.chinese).toLowerCase();
    const combined = [english, chinese].filter(Boolean).join(" / ");
    let cursor = 0;
    let ok = true;
    const positions = [];

    for (const part of parts) {
      const pos = combined.indexOf(part, cursor);
      if (pos === -1) {
        ok = false;
        break;
      }
      positions.push(pos);
      cursor = pos + part.length;
    }

    if (!ok) return;

    let score = 0;
    let seeIndex = combined.indexOf(" see ");
    if (seeIndex === -1) seeIndex = combined.indexOf(" see also ");
    if (seeIndex === -1) seeIndex = combined.length;

    const finalPos = positions[positions.length - 1];
    if (finalPos < seeIndex) score += 100;
    if (finalPos === 0) score += 50;
    if (combined.startsWith(parts[0])) score += 30;
    if (combined.startsWith(parts[parts.length - 1])) score += 20;
    if (finalPos <= combined.indexOf(parts[0]) + parts[0].length + 20) score += 10;

    scored.push([score, index]);
  });

  if (!scored.length) return rows.slice(0, 0);
  scored.sort((left, right) => right[0] - left[0] || left[1] - right[1]);
  return [rows[scored[0][1]]];
}

function findHierarchicalTargetRows(rows, parts) {
  if (!parts.length) return rows.slice(0, 0);

  for (let index = 0; index < rows.length; index += 1) {
    if (!rowMatchesTarget(rows[index], parts[0], true)) continue;

    let currentIndex = index;
    let success = true;
    for (const part of parts.slice(1)) {
      const targetLower = part.toLowerCase();
      const startLevel = parseIntSafe(rows[currentIndex].level, 0);
      let foundIndex = null;
      let cursor = currentIndex;
      while (cursor + 1 < rows.length) {
        cursor += 1;
        const nextLevel = parseIntSafe(rows[cursor].level, 0);
        if (nextLevel <= startLevel) break;
        if (rowMatchesTarget(rows[cursor], targetLower)) {
          foundIndex = cursor;
          break;
        }
      }

      if (foundIndex === null) {
        if (rowMatchesTarget(rows[currentIndex], targetLower)) continue;
        success = false;
        break;
      }
      currentIndex = foundIndex;
    }

    if (success) return [rows[currentIndex]];
  }

  return rows.slice(0, 0);
}

function filterRows(rows, pageMin, pageMax, levelMin, levelMax, fileFilters) {
  return rows.filter((row) => {
    const page = parseIntSafe(row.page, -1);
    const level = parseIntSafe(row.level, -1);
    if (pageMin !== null && page < pageMin) return false;
    if (pageMax !== null && page > pageMax) return false;
    if (levelMin !== null && level < levelMin) return false;
    if (levelMax !== null && level > levelMax) return false;
    if (fileFilters.length && !fileFilters.includes(row._source_file)) return false;
    return true;
  });
}

function searchRows(rows, query, mode, fields) {
  const queryText = query.trim();
  if (!queryText) {
    const browseRows = rows.filter((row) => parseIntSafe(row.level, 0) === 0);
    const resultRows = browseRows.map((row) => {
      const rowIndex = rows.indexOf(row);
      return rowToJson(row, false, rowIndex >= 0 ? hasDescendants(rowIndex, rows) : false);
    });
    return { resultRows, treeRows: resultRows };
  }

  const isCodeQuery = /^\d+(?:\.\d+)?$/.test(queryText);
  const queryLower = queryText.toLowerCase();
  let results = [];

  if (isCodeQuery || mode === "code") {
    const exactResults = rows.filter((row) => row._code_lower === queryLower);
    if (exactResults.length) {
      results = [...exactResults].sort((left, right) => {
        const pageDelta = parseIntSafe(left.page, 0) - parseIntSafe(right.page, 0);
        if (pageDelta !== 0) return pageDelta;
        const levelDelta = parseIntSafe(left.level, 0) - parseIntSafe(right.level, 0);
        if (levelDelta !== 0) return levelDelta;
        const fileDelta = compareText(left._source_file, right._source_file);
        if (fileDelta !== 0) return fileDelta;
        return parseIntSafe(left._source_line, 0) - parseIntSafe(right._source_line, 0);
      });
    } else {
      results = rows.filter((row) => row._code_lower.startsWith(queryLower));
    }
  } else {
    let textValues;
    if (fields.join(",") === DATA_COLUMNS.join(",")) {
      textValues = rows.map((row) => row._search_blob);
    } else {
      textValues = rows.map((row) => {
        const selected = fields.map((field) => row[`_${field}_lower`] || "").filter(Boolean);
        return selected.join(" ").trim();
      });
    }

    if (mode === "phrase") {
      results = rows.filter((row, index) => textValues[index].includes(queryLower));
    } else if (mode === "any") {
      const tokens = queryLower.split(/\s+/).filter(Boolean);
      results = tokens.length ? rows.filter((row, index) => tokens.some((token) => textValues[index].includes(token))) : rows.slice();
    } else {
      results = rows.filter((row, index) => textValues[index].includes(queryLower));
    }
  }

  const resultRows = results.map((row) => {
    const rowIndex = rows.indexOf(row);
    return rowToJson(row, true, rowIndex >= 0 ? hasDescendants(rowIndex, rows) : false);
  });
  const treeRows = queryText
    ? collectRelevantRows(results, rows).filter((row) => row.level !== 0)
    : rows.map((row, index) => rowToJson(row, false, hasDescendants(index, rows)));

  return { resultRows, treeRows };
}

async function getDataset(env) {
  if (!datasetPromise) {
    datasetPromise = env.ASSETS.fetch(new Request(DATASET_URL)).then(async (response) => {
      if (!response.ok) {
        throw new Error(`Dataset asset unavailable: ${response.status}`);
      }
      return response.json();
    });
  }
  return datasetPromise;
}

async function getPdfManifest(env) {
  if (!pdfManifestPromise) {
    pdfManifestPromise = env.ASSETS.fetch(new Request(PDF_MANIFEST_URL)).then(async (response) => {
      if (!response.ok) {
        return { available: false, storage: "r2", key: TABULAR_PDF_KEY, total_size: 0 };
      }
      return response.json();
    });
  }
  return pdfManifestPromise;
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
  if (request.method !== "GET") {
    return createResponse();
  }

  const cache = caches.default;
  const cacheKey = new Request(request.url, { method: "GET" });
  const cached = await cache.match(cacheKey);
  if (cached) {
    return addCacheHeaders(cached, seconds, { "x-cache": "HIT" });
  }

  const response = addCacheHeaders(await createResponse(), seconds, { "x-cache": "MISS" });
  if (response.ok) {
    await cache.put(cacheKey, response.clone());
  }
  return response;
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

  const dataset = await getDataset(env);
  const filteredRows = filterRows(dataset.rows, pageMin, pageMax, levelMin, levelMax, fileFilters);
  const { resultRows, treeRows } = searchRows(filteredRows, query, mode, fields);

  return jsonResponse({
    query,
    count: resultRows.length,
    rows: resultRows,
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

  const dataset = await getDataset(env);
  const rows = dataset.rows;
  let results = [];

  if (/^\d+(?:\.\d+)?$/.test(lowerTarget)) {
    results = rows.filter((row) => row._code_lower === lowerTarget);
  } else {
    const maskCn = rows.filter((row) => row._chinese_lower.trim() === lowerTarget);
    const maskEn = rows.filter((row) => row._english_lower.trim() === lowerTarget);
    results = uniqueRows(maskCn.concat(maskEn));

    if (!results.length) {
      const startsCn = rows.filter((row) => row._chinese_lower.trim().startsWith(lowerTarget));
      const startsEn = rows.filter((row) => row._english_lower.trim().startsWith(lowerTarget));
      if (startsCn.length || startsEn.length) {
        results = uniqueRows(startsCn.concat(startsEn));
      } else if (lowerTarget.includes(",")) {
        const parts = lowerTarget.split(/[，,]/).map((part) => part.trim()).filter(Boolean);
        if (parts.length > 1) {
          results = findHierarchicalTargetRows(rows, parts);
          if (!results.length) {
            results = findOrderedTargetRows(rows, parts);
          }
        }
      } else {
        const match = lowerTarget.match(/(\d+(?:\.\d+)?)$/);
        if (match) {
          const code = match[1];
          results = rows.filter((row) => row._code_lower === code);
        }
      }
    }
  }

  const resultRows = results.map((row) => {
    const rowIndex = rows.indexOf(row);
    return rowToJson(row, mark, rowIndex >= 0 ? hasDescendants(rowIndex, rows) : false);
  });
  let treeRows = results.length ? collectRelevantRows(results, rows) : [];
  if (treeRows.length) {
    treeRows = treeRows.filter((row) => row.level !== 0);
  }

  if (!mark) {
    for (const row of resultRows) row.matched = false;
    for (const row of treeRows) row.matched = false;
  }

  return jsonResponse({ query: target, count: resultRows.length, rows: resultRows, tree: buildHierarchy(treeRows) });
}

async function handleChildren(request, env) {
  const url = new URL(request.url);
  const nodeId = url.searchParams.get("id") || "";
  const query = url.searchParams.get("q") || "";
  const mode = url.searchParams.get("mode") || "auto";
  const fieldList = url.searchParams.get("fields") || "chinese,english,code";
  let fields = fieldList.split(",").filter((field) => DATA_COLUMNS.includes(field));
  if (!fields.length) fields = ["chinese", "english", "code"];

  const dataset = await getDataset(env);
  const rows = dataset.rows;
  const startIndex = rows.findIndex((row) => getRowId(row) === nodeId);
  if (startIndex === -1) {
    return jsonResponse({ children: [] });
  }

  const descendants = [];
  const startLevel = parseIntSafe(rows[startIndex].level, 0);
  const queryLower = query.trim().toLowerCase();
  const isCodeQuery = /^\d+(?:\.\d+)?$/.test(queryLower);

  for (let cursor = startIndex + 1; cursor < rows.length; cursor += 1) {
    const nextLevel = parseIntSafe(rows[cursor].level, 0);
    if (nextLevel <= startLevel) break;

    const row = rows[cursor];
    let matched = false;
    if (queryLower) {
      if (isCodeQuery || mode === "code") {
        matched = normalizeCode(row.code).startsWith(queryLower);
      } else {
        const textValues = fields.map((field) => normalizeText(row[field]).toLowerCase()).join(" ").trim();
        if (mode === "phrase") {
          matched = textValues.includes(queryLower);
        } else if (mode === "any") {
          const tokens = queryLower.split(/\s+/).filter(Boolean);
          matched = tokens.length ? tokens.some((token) => textValues.includes(token)) : false;
        } else {
          matched = textValues.includes(queryLower);
        }
      }
    }

    descendants.push(rowToJson(row, matched, hasDescendants(cursor, rows)));
  }

  return jsonResponse({ children: buildHierarchy(descendants) });
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

  const dataset = await getDataset(env);
  const tabularRows = dataset.tabular || [];
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

  const pdfManifest = await getPdfManifest(env);
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
