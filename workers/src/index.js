const FEEDBACK_PROJECT_KEY = "icd9cm3-index";
const FEEDBACK_MAX_BODY_BYTES = 16_000;
const FEEDBACK_TYPES = new Set([
  "错字/OCR错误",
  "中英文对应错误",
  "主编码错误",
  "层级错误",
  "缺少词条",
  "其他",
]);

function noStoreJsonResponse(payload, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  return new Response(JSON.stringify(payload), { ...init, headers });
}

function limitedText(value, maxLength) {
  return String(value ?? "").trim().slice(0, maxLength);
}

async function handleFeedback(request, env) {
  if (request.method !== "POST") {
    return noStoreJsonResponse({ ok: false, error: "Method not allowed" }, { status: 405 });
  }
  if (!env?.DB) {
    return noStoreJsonResponse({ ok: false, error: "D1 数据库未绑定" }, { status: 500 });
  }

  const contentLength = Number(request.headers.get("content-length") || 0);
  if (Number.isFinite(contentLength) && contentLength > FEEDBACK_MAX_BODY_BYTES) {
    return noStoreJsonResponse({ ok: false, error: "请求内容过大" }, { status: 413 });
  }

  let body;
  try {
    const raw = await request.arrayBuffer();
    if (raw.byteLength > FEEDBACK_MAX_BODY_BYTES) {
      return noStoreJsonResponse({ ok: false, error: "请求内容过大" }, { status: 413 });
    }
    body = JSON.parse(new TextDecoder().decode(raw));
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Invalid object");
  } catch {
    return noStoreJsonResponse({ ok: false, error: "请求内容不是有效 JSON" }, { status: 400 });
  }

  const feedbackType = limitedText(body.feedbackType, 40);
  const proposedValue = limitedText(body.proposedValue, 500);
  const message = limitedText(body.message, 2000);
  const contact = limitedText(body.contact, 200);
  if (!FEEDBACK_TYPES.has(feedbackType)) {
    return noStoreJsonResponse({ ok: false, error: "反馈类型无效" }, { status: 400 });
  }
  if (message.length < 5) {
    return noStoreJsonResponse({ ok: false, error: "反馈说明至少需要填写 5 个字符" }, { status: 400 });
  }

  let recordData;
  try {
    const record = body.record && typeof body.record === "object" ? body.record : {};
    const normalizedRecord = { ...record };
    if (Number.isInteger(normalizedRecord.level)) normalizedRecord.hierarchy_level = normalizedRecord.level;
    recordData = JSON.stringify(normalizedRecord);
  } catch {
    return noStoreJsonResponse({ ok: false, error: "词条数据格式无效" }, { status: 400 });
  }
  if (recordData.length > 12_000) {
    return noStoreJsonResponse({ ok: false, error: "词条数据过大" }, { status: 413 });
  }

  try {
    const result = await env.DB.prepare(`
      INSERT INTO feedback (
        project_key, record_data, feedback_type, proposed_value, message,
        contact, url, user_agent, as_name, ip_address
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      FEEDBACK_PROJECT_KEY,
      recordData,
      feedbackType,
      proposedValue,
      message,
      contact,
      limitedText(body.url, 1000),
      limitedText(request.headers.get("user-agent"), 500),
      limitedText(request.cf?.asOrganization, 200),
      limitedText(request.headers.get("CF-Connecting-IP"), 64),
    ).run();

    return noStoreJsonResponse(
      { ok: true, id: result.meta?.last_row_id ?? null, message: "反馈已提交" },
      { status: 201 },
    );
  } catch (error) {
    console.error(JSON.stringify({ event: "feedback_insert_failed", message: String(error) }));
    return noStoreJsonResponse({ ok: false, error: "反馈保存失败" }, { status: 500 });
  }
}

const TABULAR_PDF_KEY = "target.pdf";
const PDF_CACHE_SECONDS = 365 * 24 * 60 * 60;

function hasPdfBucket(env) {
  return Boolean(env && env.TABULAR_PDF);
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
    if (url.pathname === "/api/feedback") return handleFeedback(request, env);
    if (url.pathname.startsWith("/api/")) return new Response("Not found", { status: 404 });
    if (url.pathname === "/tabular-pdf") return handleTabularPdf(request, env);
    return env.ASSETS.fetch(request);
  },
};
