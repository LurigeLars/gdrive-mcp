// Offline test of the gateway's Cloudflare Access JWT checks. Usage: node public/access-test.mjs file:///C:/path/to/gdrive-mcp/public/gateway/gateway.mjs
import http from 'node:http';
import crypto from 'node:crypto';
const TEAM = 'test-team.cloudflareaccess.com', AUD = 'aud-123', SECRET = 'x'.repeat(40);
const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const other = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256', use: 'sig' };
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => String(url) === `https://${TEAM}/cdn-cgi/access/certs`
  ? new Response(JSON.stringify({ keys: [jwk] }), { status: 200 }) : realFetch(url, opts);
// fake upstream MCP
http.createServer((req, res) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { ok: true, sawJwt: 'cf-access-jwt-assertion' in req.headers } })); }).listen(13000);
Object.assign(process.env, { GATEWAY_SECRET: SECRET, UPSTREAM_HOST: '127.0.0.1', UPSTREAM_PORT: '13000', PORT: '18080',
  ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, ACCESS_ALLOWED_EMAILS: 'operator@example.com' });
await import(process.argv[2]);
await new Promise(r => setTimeout(r, 300));
const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64url');
function jwt(claims, { key = privateKey, kid = 'k1', alg = 'RS256' } = {}) {
  const h = b64({ alg, kid, typ: 'JWT' }), p = b64(claims);
  return `${h}.${p}.${crypto.sign('RSA-SHA256', Buffer.from(`${h}.${p}`), key).toString('base64url')}`;
}
const now = Math.floor(Date.now() / 1000);
const good = { aud: [AUD], iss: `https://${TEAM}`, exp: now + 600, iat: now, email: 'operator@example.com' };
const call = async (path, token) => (await fetch(`http://127.0.0.1:18080${path}`, { method: 'POST', headers: { 'content-type': 'application/json', ...(token ? { 'cf-access-jwt-assertion': token } : {}) }, body: '{"jsonrpc":"2.0","id":1,"method":"ping"}' }));
const cases = [
  ['no token on /mcp', '/mcp', null, 403],
  ['no token on secret path', `/${SECRET}/mcp`, null, 403],
  ['valid token /mcp', '/mcp', jwt(good), 200],
  ['valid token secret path', `/${SECRET}/mcp`, jwt(good), 200],
  ['wrong audience', '/mcp', jwt({ ...good, aud: ['other'] }), 403],
  ['wrong issuer', '/mcp', jwt({ ...good, iss: 'https://evil.cloudflareaccess.com' }), 403],
  ['expired', '/mcp', jwt({ ...good, exp: now - 120 }), 403],
  ['other email', '/mcp', jwt({ ...good, email: 'someone@else.com' }), 403],
  ['forged signature', '/mcp', jwt(good, { key: other.privateKey }), 403],
  ['alg none', '/mcp', `${b64({ alg: 'none', kid: 'k1' })}.${b64(good)}.`, 403],
  ['unknown kid', '/mcp', jwt(good, { kid: 'nope' }), 403],
  ['wrong path', '/other', jwt(good), 404],
];
let fail = 0;
for (const [name, path, token, want] of cases) {
  const r = await call(path, token); const body = await r.text();
  const ok = r.status === want && (want !== 200 || JSON.parse(body).result.sawJwt === false);
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${r.status}${want === 200 ? ' jwt-stripped=' + (JSON.parse(body).result?.sawJwt === false) : ''}`);
}
// Without ACCESS_AUD the gateway must refuse to start unless ALLOW_SECRET_PATH=1 is set on purpose.
const { spawnSync } = await import('node:child_process');
const { fileURLToPath } = await import('node:url');
const bare = { ...process.env, ACCESS_AUD: '', ACCESS_TEAM_DOMAIN: '', PORT: '18081', ALLOW_SECRET_PATH: '' };
const refused = spawnSync(process.execPath, [fileURLToPath(process.argv[2])], { env: bare, timeout: 5000, encoding: 'utf8' });
const okRefuse = refused.status === 1 && /ACCESS_AUD/.test(refused.stderr);
if (!okRefuse) fail++;
console.log(`${okRefuse ? 'PASS' : 'FAIL'} refuses to start without Access: exit ${refused.status}`);
const allowed = spawnSync(process.execPath, [fileURLToPath(process.argv[2])], { env: { ...bare, ALLOW_SECRET_PATH: '1' }, timeout: 1500, encoding: 'utf8' });
const okAllowed = allowed.signal === 'SIGTERM' && /listening/.test(allowed.stdout); // still running when the timeout killed it
if (!okAllowed) fail++;
console.log(`${okAllowed ? 'PASS' : 'FAIL'} starts with ALLOW_SECRET_PATH=1`);
process.exit(fail ? 1 : 0);
