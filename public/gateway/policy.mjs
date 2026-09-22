// Gateway policy for the gdrive MCP server. Node standard library only.
// The server enforces the Drive boundary itself; the gateway adds a tool allowlist as a second fence.
export const DEFAULT_ALLOWED_TOOLS = [
  'drive_search', 'drive_semantic_search', 'drive_recent', 'drive_list', 'drive_get', 'drive_read',
  'drive_create', 'drive_update_text', 'drive_rename', 'drive_move', 'drive_copy', 'drive_trash',
  'drive_untrash', 'drive_share', 'drive_unshare', 'drive_comments', 'drive_comment',
  'docs_get', 'docs_edit', 'docs_append_markdown', 'docs_insert_table',
  'sheets_get', 'sheets_read', 'sheets_write', 'sheets_append', 'sheets_edit',
].join(',');

export function parseAllowedTools(value) {
  return new Set((value || DEFAULT_ALLOWED_TOOLS).split(',').map(s => s.trim()).filter(Boolean));
}

export const wantsCompactResult = () => false;

// Filters tools/list to the allowlist. Server instructions pass through unchanged.
export function rewriteResponse(msg, { allowedTools }) {
  if (msg?.result?.tools) msg.result.tools = msg.result.tools.filter(t => allowedTools.has(t.name));
  return msg;
}

export function checkRequest(msg, allowedTools) {
  if (msg?.method !== 'tools/call') return {};
  if (!allowedTools.has(msg.params?.name)) return { error: `Tool not available on this server: ${msg.params?.name}` };
  return {};
}

export const rpcError = (id, message) => ({ jsonrpc: '2.0', id: id ?? null, error: { code: -32601, message } });
export const rpcToolError = (id, message) => ({ jsonrpc: '2.0', id: id ?? null, result: { content: [{ type: 'text', text: message }], isError: true } });
