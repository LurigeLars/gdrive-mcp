// Public gatekeeper in front of the gdrive MCP server (HTTP stream mode on the host, 127.0.0.1).
// Copied from firecrawl-local/public/gateway/gateway.mjs; only policy.mjs and the Origin/credential header
// stripping differ.
// - Only POST/GET/DELETE /mcp is accepted; an optional legacy /<GATEWAY_SECRET>/mcp alias may also be used.
//   Both routes always require a valid Cloudflare Access JWT.
// - Cloudflare Access is mandatory for every MCP request.
// - Rewrites to the upstream endpoint (/mcp) with Host set to the upstream.
// - Only tools in ALLOWED_TOOLS may be called, and tools/list is filtered to them.
// - initialize responses get their server instructions replaced by instructions.md (next to this file),
//   so clients like ChatGPT pick up usage guidance when the connector is refreshed.
// - Scrapes that only ask for query/json/summary get their page metadata trimmed (see policy.mjs).
//   Tool rules are shared with the local stdio proxy (local-mcp/stdio-proxy.mjs) through policy.mjs.
// - Fixed-window rate limit per client IP, request body size cap.
// Node standard library only.
import http from 'node:http';
import crypto from 'node:crypto';
import {
  parseAllowedTools, checkRequest, wantsCompactResult, rewriteResponse, rpcError as rpcErrorMsg, rpcToolError,
} from './policy.mjs';

const SECRET = process.env.GATEWAY_SECRET ?? '';
if (SECRET && !/^[A-Za-z0-9_-]{32,128}$/.test(SECRET)) {
  console.error('GATEWAY_SECRET must be 32-128 URL-safe characters when configured; refusing to start');
  process.exit(1);
}
const UPSTREAM_HOST = process.env.UPSTREAM_HOST ?? 'mcp';
const UPSTREAM_PORT = Number(process.env.UPSTREAM_PORT ?? 3000);
const UPSTREAM_PATH = process.env.UPSTREAM_PATH ?? '/mcp';
const PORT = Number(process.env.PORT ?? 8080);
const RATE_PER_MIN = Number(process.env.RATE_PER_MIN ?? 120);
const MAX_BODY = 256 * 1024;
const ALLOWED_TOOLS = parseAllowedTools(process.env.ALLOWED_TOOLS);

// Cloudflare Access is mandatory. Deployment configuration may select the team/audience,
// but it can no longer disable authentication or fall back to a secret-link-only mode.
const ACCESS_TEAM_DOMAIN = process.env.ACCESS_TEAM_DOMAIN ?? '';
const ACCESS_AUD = process.env.ACCESS_AUD ?? '';
const ACCESS_EMAILS = new Set((process.env.ACCESS_ALLOWED_EMAILS ?? '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean));
if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/i.test(ACCESS_TEAM_DOMAIN)) {
  console.error('ACCESS_TEAM_DOMAIN must be a Cloudflare Access team domain (*.cloudflareaccess.com); refusing to start');
  process.exit(1);
}
if (!ACCESS_AUD || ACCESS_AUD.length > 512 || /\s/.test(ACCESS_AUD)) {
  console.error('ACCESS_AUD is required and must be a single non-whitespace audience value; refusing to start');
  process.exit(1);
}
const ACCESS_ISSUER = `https://${ACCESS_TEAM_DOMAIN}`;

const jwks = { keys: new Map(), fetchedAt: 0 };
async function accessKey(kid) {
  const stale = Date.now() - jwks.fetchedAt > 3_600_000;
  const canRefresh = Date.now() - jwks.fetchedAt > 30_000;
  if ((stale || !jwks.keys.has(kid)) && canRefresh) {
    jwks.fetchedAt = Date.now();
    const r = await fetch(`${ACCESS_ISSUER}/cdn-cgi/access/certs`, { signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw new Error(`certs HTTP ${r.status}`);
    const { keys = [] } = await r.json();
    jwks.keys = new Map(keys.map(k => [k.kid, crypto.createPublicKey({ key: k, format: 'jwk' })]));
  }
  return jwks.keys.get(kid);
}

const b64json = s => JSON.parse(Buffer.from(s, 'base64url').toString('utf8'));
async function verifyAccessJwt(token) {
  const parts = (token ?? '').split('.');
  if (parts.length !== 3) return { ok: false, reason: 'missing token' };
  try {
    const header = b64json(parts[0]);
    const claims = b64json(parts[1]);
    if (header.alg !== 'RS256') return { ok: false, reason: `alg ${header.alg}` };
    const key = await accessKey(header.kid);
    if (!key) return { ok: false, reason: 'unknown kid' };
    const valid = crypto.verify('RSA-SHA256', Buffer.from(`${parts[0]}.${parts[1]}`), key, Buffer.from(parts[2], 'base64url'));
    if (!valid) return { ok: false, reason: 'bad signature' };
    const now = Date.now() / 1000;
    const aud = [claims.aud].flat();
    if (!aud.includes(ACCESS_AUD)) return { ok: false, reason: 'wrong audience' };
    if (claims.iss !== ACCESS_ISSUER) return { ok: false, reason: 'wrong issuer' };
    if (typeof claims.exp !== 'number' || claims.exp < now - 30) return { ok: false, reason: 'expired' };
    if (typeof claims.nbf === 'number' && claims.nbf > now + 30) return { ok: false, reason: 'not yet valid' };
    const email = String(claims.email ?? '').toLowerCase();
    if (ACCESS_EMAILS.size && !ACCESS_EMAILS.has(email)) return { ok: false, reason: `email not allowed: ${email || '(none)'}` };
    return { ok: true, email };
  } catch (err) {
    return { ok: false, reason: `verify error: ${err.message}` };
  }
}

const legacyExpected = SECRET ? Buffer.from(`/${SECRET}/mcp`) : null;
function pathAllowed(url) {
  const path = Buffer.from((url ?? '').split('?')[0]);
  if (path.toString() === '/mcp') return true;
  return legacyExpected !== null &&
    path.length === legacyExpected.length &&
    crypto.timingSafeEqual(path, legacyExpected);
}

const windows = new Map(); // ip -> { start, count }
function rateLimited(ip) {
  const now = Date.now();
  const w = windows.get(ip);
  if (!w || now - w.start >= 60_000) {
    windows.set(ip, { start: now, count: 1 });
    return false;
  }
  return ++w.count > RATE_PER_MIN;
}
setInterval(() => {
  const now = Date.now();
  for (const [ip, w] of windows) if (now - w.start >= 60_000) windows.delete(ip);
}, 60_000).unref();

function clientIp(req) {
  // cloudflared sets CF-Connecting-IP; only cloudflared can reach this port.
  return req.headers['cf-connecting-ip'] ?? req.socket.remoteAddress ?? 'unknown';
}

function send(res, status, body = '') {
  res.writeHead(status, { 'content-type': 'text/plain' });
  res.end(body);
}

function sendJson(res, obj) {
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify(obj));
}

const rewriteJsonText = (text, ctx) => {
  try {
    const parsed = JSON.parse(text);
    return JSON.stringify(Array.isArray(parsed) ? parsed.map(m => rewriteResponse(m, ctx)) : rewriteResponse(parsed, ctx));
  } catch { return text; }
};
const rewriteSseLine = (line, ctx) => {
  if (!line.startsWith('data:')) return line;
  const rewritten = rewriteJsonText(line.slice(5), ctx);
  return rewritten === line.slice(5) ? line : `data: ${rewritten}`;
};

// `ctx` = { allowedTools, compactIds } or null (pass through untouched).
// SSE is rewritten line by line as it streams, so keepalives still flow during slow local-model calls
// (Cloudflare drops responses that stay silent for about 100 s). Plain JSON is small and buffered.
function forward(req, res, body, ctx) {
  const headers = { ...req.headers, host: `${UPSTREAM_HOST}:${UPSTREAM_PORT}` };
  delete headers['content-length'];
  if (body) headers['content-length'] = String(body.length);
  // Never pass client credentials upstream; the upstream uses its own Google token.
  delete headers.authorization;
  delete headers['x-api-key'];
  delete headers['cf-access-jwt-assertion'];
  delete headers.cookie;
  // Server-to-server call: the upstream rejects browser origins (DNS-rebinding protection).
  delete headers.origin;
  delete headers.referer;

  const up = http.request(
    { host: UPSTREAM_HOST, port: UPSTREAM_PORT, method: req.method, path: UPSTREAM_PATH, headers },
    upRes => {
      if (!ctx) {
        res.writeHead(upRes.statusCode ?? 502, upRes.headers);
        upRes.pipe(res);
        return;
      }
      const headers = { ...upRes.headers };
      delete headers['content-length'];
      if ((upRes.headers['content-type'] ?? '').includes('text/event-stream')) {
        res.writeHead(upRes.statusCode ?? 502, headers);
        let pending = '';
        upRes.setEncoding('utf8');
        upRes.on('data', chunk => {
          pending += chunk;
          const lines = pending.split('\n');
          pending = lines.pop();
          if (lines.length) res.write(lines.map(l => rewriteSseLine(l, ctx)).join('\n') + '\n');
        });
        upRes.on('end', () => res.end(pending ? rewriteSseLine(pending, ctx) : undefined));
        return;
      }
      const chunks = [];
      upRes.on('data', c => chunks.push(c));
      upRes.on('end', () => {
        const out = Buffer.from(rewriteJsonText(Buffer.concat(chunks).toString('utf8'), ctx));
        delete headers['transfer-encoding'];
        headers['content-length'] = String(out.length);
        res.writeHead(upRes.statusCode ?? 502, headers);
        res.end(out);
      });
    },
  );
  up.on('error', err => {
    console.warn('upstream error');
    if (!res.headersSent) send(res, 502, 'upstream unavailable'); else res.destroy();
  });
  // Abort upstream only if the client goes away before we finish (req 'close' fires once the body is read).
  res.on('close', () => { if (!res.writableFinished) up.destroy(); });
  up.end(body);
}

http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/healthz') return send(res, 200, 'ok');
  if (!pathAllowed(req.url)) return send(res, 404);
  let ip = clientIp(req);
  const v = await verifyAccessJwt(req.headers['cf-access-jwt-assertion']);
  if (!v.ok) {
    console.warn('access denied');
    return send(res, 403, 'forbidden');
  }
  if (v.email) ip = v.email;
  if (rateLimited(ip)) {
    console.warn('rate limited');
    return send(res, 429, 'rate limited');
  }
  if (req.method !== 'POST') return forward(req, res, null, null);

  const chunks = [];
  let size = 0;
  req.on('data', c => {
    size += c.length;
    if (size > MAX_BODY) { send(res, 413); req.destroy(); return; }
    chunks.push(c);
  });
  req.on('end', () => {
    if (size > MAX_BODY) return;
    const body = Buffer.concat(chunks);
    let msgs;
    try { msgs = [JSON.parse(body.toString('utf8'))].flat(); } catch { return send(res, 400, 'invalid json'); }
    let needsRewrite = false;
    const compactIds = new Set();
    for (const m of msgs) {
      if (m?.method === 'tools/list' || m?.method === 'initialize') needsRewrite = true;
      const verdict = checkRequest(m, ALLOWED_TOOLS);
      if (verdict.error) {
        console.warn('blocked tool call');
        return sendJson(res, rpcErrorMsg(m.id, verdict.error));
      }
      if (verdict.toolError) {
        console.warn('refused unsupported tool options');
        return sendJson(res, rpcToolError(m.id, verdict.toolError));
      }
      if (m?.method === 'tools/call' && wantsCompactResult(m.params)) { compactIds.add(m.id); needsRewrite = true; }
    }
    console.log(`${new Date().toISOString()} request accepted`);
    forward(req, res, body, needsRewrite ? { allowedTools: ALLOWED_TOOLS, compactIds } : null);
  });
}).listen(PORT, '0.0.0.0', () => console.log(`gateway listening on ${PORT}, cloudflare access: required`));
