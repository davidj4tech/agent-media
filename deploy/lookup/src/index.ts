// sasonica.com/r/<install id> — where a Sasonica server is today.
//
// A stranger's server reaches the phone through a Cloudflare quick tunnel
// (`*.trycloudflare.com`), whose name changes every time the tunnel restarts.
// The server PUTs its current URL here, signed with the Ed25519 key it made at
// install; the app, holding the install id from its pairing, GETs it when its
// stored address stops answering. One KV entry per install. It sees addresses
// only, never traffic (agent-media docs/proposals/2026-10-03-off-the-tailnet.md).
//
//   GET /r/<id>   → 200 {"url", "updated"} | 404
//   PUT /r/<id>   body {"url", "ts", "pub"} (JSON, ≤ 1 KB),
//                 header X-Sasonica-Signature: base64url(Ed25519(body))
//
// The id IS the key: base32(sha256(pub)[:16]), lower case, no padding. So the
// first write cannot be squatted — only the holder of the key whose hash is the
// id can ever write it — and the stored key is the one the first PUT showed
// (trust on first use, and every later PUT must match it too).

export interface Env {
  LOOKUP: KVNamespace;
  WRITES: RateLimit;
  /** The only hosts a URL may name, as suffixes (comma-separated). */
  ALLOW_SUFFIXES: string;
}

interface Entry {
  url: string;
  ts: number;        // the server's clock, seconds; only ever goes forward
  pub: string;       // base64url, 32 bytes
  updated: number;   // ours, seconds
  recent: number[];  // our times of the writes in the last hour
}

const MAX_BODY = 1024;
const SKEW_S = 600;               // a signed write is good for ±10 min
const MIN_GAP_S = 5;              // per install
const MAX_PER_HOUR = 30;          // per install
const TTL_S = 60 * 24 * 3600;     // a server republishes daily; gone after 60 idle days
const ID_RE = /^[a-z2-7]{26}$/;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Sasonica-Signature",
  "Access-Control-Max-Age": "86400",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...CORS },
  });
}

function b64urlDecode(s: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]*$/.test(s)) return null;
  const pad = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  try {
    return Uint8Array.from(atob(pad), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

function base32(bytes: Uint8Array): string {
  const abc = "abcdefghijklmnopqrstuvwxyz234567";
  let bits = 0, value = 0, out = "";
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += abc[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += abc[(value << (5 - bits)) & 31];
  return out;
}

export async function installId(pub: Uint8Array): Promise<string> {
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", pub));
  return base32(h.slice(0, 16));
}

function urlAllowed(url: string, env: Env): boolean {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== "https:" || u.port || u.username || u.password) return false;
  if (u.pathname !== "/" || u.search || u.hash) return false;
  if (url !== u.origin) return false;   // exactly https://host, nothing else
  const host = u.hostname;
  return (env.ALLOW_SUFFIXES || ".trycloudflare.com")
    .split(",").map((s) => s.trim()).filter(Boolean)
    .some((sfx) => host.endsWith(sfx) && /^[a-z0-9-]{1,63}$/.test(host.slice(0, -sfx.length)));
}

async function put(req: Request, env: Env, id: string): Promise<Response> {
  const ip = req.headers.get("CF-Connecting-IP") || "?";
  const { success } = await env.WRITES.limit({ key: ip });
  if (!success) return json(429, { error: "too many writes; slow down" });
  if (Number(req.headers.get("Content-Length") || "0") > MAX_BODY) {
    return json(413, { error: "body too large" });
  }
  const raw = new Uint8Array(await req.arrayBuffer());
  if (raw.length > MAX_BODY) return json(413, { error: "body too large" });
  const sig = b64urlDecode(req.headers.get("X-Sasonica-Signature") || "");
  if (!sig || sig.length !== 64) return json(401, { error: "no signature" });

  let body: { url?: unknown; ts?: unknown; pub?: unknown };
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json(400, { error: "not JSON" });
  }
  const pub = typeof body.pub === "string" ? b64urlDecode(body.pub) : null;
  if (!pub || pub.length !== 32) return json(400, { error: "pub must be a 32-byte key" });
  if (typeof body.url !== "string" || !urlAllowed(body.url, env)) {
    return json(400, { error: "url must be https://<name>.trycloudflare.com" });
  }
  const ts = typeof body.ts === "number" && Number.isFinite(body.ts) ? body.ts : NaN;
  const now = Date.now() / 1000;
  if (!(Math.abs(now - ts) <= SKEW_S)) return json(400, { error: "ts is not now (check the clock)" });

  if ((await installId(pub)) !== id) return json(403, { error: "this key does not own this id" });
  let ok = false;
  try {
    const key = await crypto.subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
    ok = await crypto.subtle.verify({ name: "Ed25519" }, key, sig, raw);
  } catch {
    ok = false;
  }
  if (!ok) return json(401, { error: "bad signature" });

  const old = await env.LOOKUP.get<Entry>(id, "json");
  if (old) {
    if (old.pub !== body.pub) return json(403, { error: "this id has another key" });
    if (ts <= old.ts) return json(409, { error: "an older write than the one kept" });
  }
  const recent = (old?.recent || []).filter((t) => now - t < 3600);
  if (recent.length && now - recent[recent.length - 1] < MIN_GAP_S) {
    return json(429, { error: "too soon after the last write" });
  }
  if (recent.length >= MAX_PER_HOUR) return json(429, { error: "too many writes this hour" });
  recent.push(Math.round(now * 1000) / 1000);
  const entry: Entry = { url: body.url, ts, pub: body.pub as string, updated: Math.round(now), recent };
  await env.LOOKUP.put(id, JSON.stringify(entry), { expirationTtl: TTL_S });
  return json(200, { ok: true, url: entry.url, updated: entry.updated });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const path = new URL(req.url).pathname;
    const m = /^\/r\/([^/]+)\/?$/.exec(path);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (!m || !ID_RE.test(m[1])) return json(404, { error: "no such install" });
    const id = m[1];
    if (req.method === "GET" || req.method === "HEAD") {
      const got = await env.LOOKUP.get<Entry>(id, { type: "json", cacheTtl: 30 });
      if (!got) return json(404, { error: "no such install" });
      return json(200, { url: got.url, updated: got.updated });
    }
    if (req.method === "PUT" || req.method === "POST") return put(req, env, id);
    return json(405, { error: "GET or PUT" });
  },
} satisfies ExportedHandler<Env>;
