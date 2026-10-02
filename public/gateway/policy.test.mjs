import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_ALLOWED_TOOLS,
  parseAllowedTools,
  rewriteResponse,
  checkRequest,
} from './policy.mjs';

const expected = [
  'drive_search',
  'drive_semantic_search',
  'drive_get',
  'drive_read',
  'drive_update_text',
  'drive_append_text',
  'drive_trash',
  'drive_untrash',
  'drive_share',
  'drive_unshare',
];

test('default ChatGPT DriveMCP surface is specialized and non-overlapping', () => {
  assert.deepEqual(DEFAULT_ALLOWED_TOOLS.split(','), expected);
  assert.equal(DEFAULT_ALLOWED_TOOLS.includes('docs_edit'), false);
  assert.equal(DEFAULT_ALLOWED_TOOLS.includes('sheets_read'), false);
  assert.equal(DEFAULT_ALLOWED_TOOLS.includes('drive_move'), false);
  assert.equal(DEFAULT_ALLOWED_TOOLS.includes('drive_create'), false);
});

test('tools/list exposes only the specialized DriveMCP subset', () => {
  const all = [
    ...expected,
    'drive_recent',
    'drive_list',
    'drive_create',
    'drive_move',
    'drive_copy',
    'drive_comments',
    'docs_get',
    'docs_edit',
    'sheets_get',
    'sheets_read',
  ].map(name => ({ name }));
  const out = rewriteResponse(
    { result: { tools: all } },
    { allowedTools: parseAllowedTools() },
  );
  assert.deepEqual(out.result.tools.map(tool => tool.name), expected);
});

test('gateway rejects hidden overlap tools but accepts specialized tools', () => {
  const allowed = parseAllowedTools();
  assert.deepEqual(
    checkRequest({ method: 'tools/call', params: { name: 'drive_read' } }, allowed),
    {},
  );
  assert.match(
    checkRequest({ method: 'tools/call', params: { name: 'docs_edit' } }, allowed).error,
    /not available/,
  );
  assert.match(
    checkRequest({ method: 'tools/call', params: { name: 'drive_move' } }, allowed).error,
    /not available/,
  );
});
