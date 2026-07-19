const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "if-modified-since",
  "if-none-match",
  "if-range",
  "range",
  "user-agent",
];

const FORWARDED_RESPONSE_HEADERS = [
  "accept-ranges",
  "content-disposition",
  "content-encoding",
  "content-language",
  "content-length",
  "content-range",
  "content-security-policy",
  "content-type",
  "etag",
  "last-modified",
  "vary",
  "x-frame-options",
];

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

function originRequest(request) {
  const incoming = new URL(request.url);
  const target = new URL("http://avian-origin.internal");
  target.pathname = incoming.pathname;
  target.search = incoming.search;

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }

  return new Request(target, {
    method: request.method,
    headers,
    redirect: "manual",
  });
}

function unavailableResponse() {
  return new Response("Archive temporarily unavailable\n", {
    status: 502,
    headers: {
      "cache-control": "private, no-store",
      "content-type": "text/plain; charset=utf-8",
      "x-content-type-options": "nosniff",
      "x-robots-tag": "noindex, nofollow, noarchive",
    },
  });
}

function publicResponse(request, upstream) {
  if (REDIRECT_STATUSES.has(upstream.status)) return unavailableResponse();

  const headers = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  headers.set("cache-control", "private, no-store");
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-robots-tag", "noindex, nofollow, noarchive");

  return new Response(request.method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed\n", {
        status: 405,
        headers: {
          allow: "GET, HEAD",
          "cache-control": "private, no-store",
          "content-type": "text/plain; charset=utf-8",
          "x-content-type-options": "nosniff",
          "x-robots-tag": "noindex, nofollow, noarchive",
        },
      });
    }

    try {
      const upstream = await env.AVIAN_ORIGIN.fetch(originRequest(request));
      return publicResponse(request, upstream);
    } catch (error) {
      console.error("Avian origin unavailable", error);
      return unavailableResponse();
    }
  },
};
