// Dynasty Model proxy worker.
//
// Proxies myfantasyleague.com so the GitHub Pages site can fetch a user's
// league data client-side. (MFL's CORS only allows their own subdomains, so
// browser fetches from pstiehl.github.io get blocked without this in between.)
//
// Endpoints:
//   GET /mfl/<year>/export?TYPE=...&L=...&JSON=1
//      -> proxied to https://api.myfantasyleague.com/<year>/export?...
//      -> JSON returned with permissive CORS headers
//      -> cached at the edge for 5 minutes
//
//   GET /sleeper/v1/<rest...>
//      -> proxied to https://api.sleeper.app/v1/<rest...> (CORS-friendly anyway,
//         but routing through the worker lets us add edge caching to slow
//         endpoints like transactions/<week>)
//
//   GET /health
//      -> { "status": "ok" } sanity check
//
// Deploy:
//   cd scripts/cf-worker
//   npx wrangler deploy
//
// You'll need a Cloudflare API token with "Workers Scripts: Edit" scope.
// See README.md in this directory.

const ALLOWED_ORIGINS = [
  "https://pstiehl.github.io",
  "https://sandpaw-ai.github.io",
  "http://localhost:8000",   // local dev
];

const MFL_HOST = "https://api.myfantasyleague.com";
const SLEEPER_HOST = "https://api.sleeper.app";

const CACHE_TTL_SECONDS = 300; // 5 min

function corsHeaders(originHeader) {
  // Echo origin only if it's on the allowlist; otherwise fall back to "null"
  // so misconfigured cross-origin reads don't silently succeed.
  const allowed = ALLOWED_ORIGINS.includes(originHeader) ? originHeader : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowed,
    "Vary": "Origin",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(body, status, originHeader) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(originHeader),
    },
  });
}

async function proxyJson(targetUrl, request) {
  const originHeader = request.headers.get("Origin") || "";
  const cacheKey = new Request(targetUrl, { method: "GET" });
  const cache = caches.default;

  let cached = await cache.match(cacheKey);
  if (cached) {
    // Re-emit with our CORS headers (the cached response may not have them
    // attached if it was stored as the upstream raw response).
    const body = await cached.text();
    return new Response(body, {
      status: cached.status,
      headers: {
        "Content-Type": "application/json",
        "X-Proxy-Cache": "HIT",
        ...corsHeaders(originHeader),
      },
    });
  }

  let upstream;
  try {
    upstream = await fetch(targetUrl, {
      headers: { "User-Agent": "dynasty-model-proxy/1.0" },
      redirect: "follow",
    });
  } catch (err) {
    return jsonResponse({ error: "upstream_fetch_failed", message: String(err) }, 502, originHeader);
  }

  const contentType = upstream.headers.get("Content-Type") || "";
  const body = await upstream.text();

  // Cache successful JSON responses.
  if (upstream.ok && contentType.includes("json")) {
    const cacheable = new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`,
      },
    });
    await cache.put(cacheKey, cacheable.clone());
  }

  return new Response(body, {
    status: upstream.status,
    headers: {
      "Content-Type": contentType || "application/json",
      "X-Proxy-Cache": "MISS",
      ...corsHeaders(originHeader),
    },
  });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const originHeader = request.headers.get("Origin") || "";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(originHeader) });
    }

    if (request.method !== "GET") {
      return jsonResponse({ error: "method_not_allowed" }, 405, originHeader);
    }

    if (url.pathname === "/health") {
      return jsonResponse({ status: "ok", time: new Date().toISOString() }, 200, originHeader);
    }

    // /mfl/<year>/export?TYPE=...&L=...&JSON=1
    const mflMatch = url.pathname.match(/^\/mfl\/(\d{4})\/export$/);
    if (mflMatch) {
      const year = mflMatch[1];
      const params = url.searchParams.toString();
      const target = `${MFL_HOST}/${year}/export?${params}`;
      return proxyJson(target, request);
    }

    // /sleeper/v1/<rest...>
    if (url.pathname.startsWith("/sleeper/v1/")) {
      const rest = url.pathname.slice("/sleeper/".length);
      const params = url.searchParams.toString();
      const target = `${SLEEPER_HOST}/${rest}${params ? "?" + params : ""}`;
      return proxyJson(target, request);
    }

    return jsonResponse({
      error: "not_found",
      hint: "GET /mfl/<year>/export?TYPE=...&L=... or /sleeper/v1/...",
    }, 404, originHeader);
  },
};
