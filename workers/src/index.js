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
    if (url.pathname.startsWith("/api/")) return new Response("Not found", { status: 404 });
    if (url.pathname === "/tabular-pdf") return handleTabularPdf(request, env);
    return env.ASSETS.fetch(request);
  },
};
