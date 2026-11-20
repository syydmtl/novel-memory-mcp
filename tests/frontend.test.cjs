const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), { encoding: 'utf8' });
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/\n\s*\/\/ Start\s*\n\s*loadData\(\);\s*$/, '\n');

function stage(id, content = 'Fictional content') {
  return { memory_id: id, title: id, date_range: 'test day', tags: [], raw_tags: '', content,
    content_preview: content.slice(0, 120), content_length: content.length,
    digest: createHash('sha256').update(content).digest('hex') };
}

function harness() {
  const elements = new Map();
  const events = new Map();
  const node = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { value: '', textContent: '', innerHTML: '', style: {}, checked: false,
        hidden: false, focus() {}, click() {},
        classList: { add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
          contains(name) { return classes.has(name); } } });
    }
    return elements.get(id);
  };
  const server = { libraries: [{ library_id: 'book_a', name: 'Book A', description: '', errors: [],
    memories: [stage('a_stage')] }], scope: { library_id: 'book_a', memory_ids: ['a_stage'] },
    requests: [], failScope: false, invalidScope: false, confirm: true };
  const respond = (status, body) => ({ status, ok: status >= 200 && status < 300,
    json: async () => JSON.parse(JSON.stringify(body)) });
  const sandbox = { console, Set, Date, Math, FormData: class {},
    sessionStorage: { removeItem(name) { delete this[name]; } },
    document: { getElementById: node }, location: { reload() {} }, setTimeout() { return 1; },
    window: { addEventListener(name, handler) { events.set(name, handler); },
      confirm() { return server.confirm; } },
    fetch: async (url, options = {}) => {
      const method = options.method || 'GET';
      const body = options.body ? JSON.parse(options.body) : null;
      server.requests.push({ url, method, body, headers: { ...options.headers } });
      if (url === '/api/libraries' && method === 'GET') return respond(200, server.libraries);
      if (url === '/api/scope' && method === 'GET') return server.invalidScope
        ? respond(503, { error: 'invalid scope' }) : respond(200, server.scope);
      if (url === '/api/libraries' && method === 'POST') {
        const book = { ...body, memories: [], errors: [] };
        server.libraries.push(book);
        return respond(201, book);
      }
      if (url === '/api/scope' && method === 'POST') {
        if (server.networkFailure) throw new Error('Synthetic network interruption');
        const book = server.libraries.find(item => item.library_id === body.library_id);
        if (server.failScope || !body.memory_ids.every(id => book.memories.some(item => item.memory_id === id))) {
          return respond(400, { error: 'scope not saved' });
        }
        server.scope = JSON.parse(JSON.stringify(body));
        server.invalidScope = false;
        return respond(200, body);
      }
      const endpoint = url.match(/^\/api\/libraries\/([^/]+)\/memories(?:\/([^/]+))?$/);
      if (endpoint) {
        const book = server.libraries.find(item => item.library_id === endpoint[1]);
        const id = endpoint[2];
        if (method === 'GET') return respond(200, book.memories.find(item => item.memory_id === id));
        if (method === 'POST') {
          const memoryId = body.memory_id || 'imported_stage';
          const old = book.memories.find(item => item.memory_id === memoryId);
          if (old && (!body.overwrite || body.expected_digest !== old.digest)) {
            return respond(409, { error: 'already exists', memory_id: memoryId });
          }
          const record = { ...stage(memoryId, body.content || body.raw_markdown), ...body, memory_id: memoryId };
          book.memories = book.memories.filter(item => item.memory_id !== memoryId).concat(record);
          return respond(201, record);
        }
        if (method === 'PUT') {
          const old = book.memories.find(item => item.memory_id === id);
          if (body.expected_digest !== old.digest) return respond(409, { error: 'record changed' });
          Object.assign(old, stage(id, body.content), body);
          return respond(200, old);
        }
        if (method === 'DELETE') {
          const old = book.memories.find(item => item.memory_id === id);
          if (body.expected_digest !== old.digest) return respond(409, { error: 'record changed' });
          book.memories = book.memories.filter(item => item.memory_id !== id);
          server.scope.memory_ids = server.scope.memory_ids.filter(item => item !== id);
          return respond(200, { ok: true });
        }
      }
      if (url === '/api/token' || url === '/api/login') return respond(200, { ok: true });
      throw new Error(`Unexpected route ${method} ${url}`);
    } };
  sandbox.window.location = sandbox.location;
  const context = vm.createContext(sandbox);
  vm.runInContext(script, context);
  return { node, server, context, events, run(code) { return vm.runInContext(code, context); } };
}

async function newBook(h) {
  h.node('btnNewNovel').onclick();
  h.node('inputNovelId').value = 'book_b';
  h.node('inputNovelName').value = 'Book B';
  await h.node('btnNovelModalSubmit').onclick();
}

async function fillNew(h) {
  await h.node('btnNewStage').onclick();
  for (const [id, value] of Object.entries({ inputMemoryId: 'b_new', inputDateRange: 'day',
    inputStageTitle: 'B new', inputStageContent: 'Fictional new record' })) h.node(id).value = value;
}

test('draft selection never claims saved mounting', async () => {
  const h = harness();
  h.server.libraries[0].memories.push(stage('second'));
  await h.run('loadData()');
  h.node('stageList').onchange({ target: { matches: () => true, value: 'second', checked: true } });
  assert.equal(h.node('mountCount').textContent, 1);
  assert.match(h.node('stageList').innerHTML, /待挂载/);
  assert.match(h.node('mountBadge').innerHTML, /尚未保存/);
  assert.deepEqual(h.server.scope.memory_ids, ['a_stage']);
});

test('a new book starts with its own empty draft and mounts only its own records', async () => {
  const h = harness();
  await h.run('loadData()');
  await newBook(h);
  assert.equal(h.run('selectedMountIds.size'), 0);
  await fillNew(h);
  await h.node('btnEditorSubmit').onclick();
  assert.equal(h.server.scope.library_id, 'book_b');
  assert.deepEqual(h.server.scope.memory_ids, ['b_new']);
  assert.equal(h.run('libs[activeLibIndex].library_id'), 'book_b');
});

test('a saved record and a failed mount produce an explicit partial-success result', async () => {
  const h = harness();
  await h.run('loadData()');
  await newBook(h);
  await fillNew(h);
  h.server.failScope = true;
  await h.node('btnEditorSubmit').onclick();
  assert.equal(h.server.libraries[1].memories.length, 1);
  assert.equal(h.server.scope.library_id, 'book_a');
  assert.equal(h.run('libs[activeLibIndex].library_id'), 'book_b');
  assert.match(h.node('toast').textContent, /正文已保存入库，但挂载失败/);
});

test('imports preserve pending checks without silently changing saved scope', async () => {
  const h = harness();
  await h.run('loadData()');
  h.context.files = [{ name: 'sample.md', type: 'text/markdown', text: async () => 'New fictional record' }];
  await h.run('handleFilesUpload(files)');
  assert.equal(h.run('selectedMountIds.has("imported_stage")'), true);
  assert.deepEqual(h.server.scope.memory_ids, ['a_stage']);
  assert.match(h.node('stageList').innerHTML, /待挂载/);
  assert.equal(h.server.requests.find(request => request.method === 'POST').body.overwrite, false);
});

test('network failure after creation cannot turn a saved record into an apparent unsaved editor', async () => {
  const h = harness();
  await h.run('loadData()');
  await newBook(h);
  await fillNew(h);
  h.server.networkFailure = true;
  await h.node('btnEditorSubmit').onclick();
  assert.equal(h.server.libraries[1].memories.length, 1);
  assert.equal(h.node('editorDrawer').classList.contains('open'), false);
  assert.match(h.node('toast').textContent, /正文已保存入库，网络中断/);
});

test('cancelled collision never overwrites and confirmed replacement submits a version', async () => {
  const h = harness();
  h.server.libraries[0].memories.push(stage('imported_stage', 'Original import'));
  await h.run('loadData()');
  h.context.files = [{ name: 'sample.md', type: 'text/markdown', text: async () => 'Replacement import' }];
  h.server.confirm = false;
  await h.run('handleFilesUpload(files)');
  assert.equal(h.server.libraries[0].memories.find(item => item.memory_id === 'imported_stage').content, 'Original import');
  h.server.confirm = true;
  await h.run('handleFilesUpload(files)');
  const writes = h.server.requests.filter(request => request.method === 'POST' && request.body.overwrite);
  assert.equal(writes.length, 1);
  assert.match(writes[0].body.expected_digest, /^[0-9a-f]{64}$/);
});

test('editing and deletion send the version originally shown to the user', async () => {
  const h = harness();
  await h.run('loadData()');
  const old = h.server.libraries[0].memories[0].digest;
  await h.run('openEditor("edit", "a_stage")');
  h.node('inputStageContent').value = 'Changed content';
  await h.node('btnEditorSubmit').onclick();
  assert.equal(h.server.requests.find(request => request.method === 'PUT').body.expected_digest, old);
  await h.node('stageList').onclick({ target: { closest: selector => selector === '.btn-delete' ? { dataset: { id: 'a_stage' } } : null } });
  const current = h.server.libraries[0].memories[0].digest;
  await h.node('btnDeleteSubmit').onclick();
  assert.equal(h.server.requests.find(request => request.method === 'DELETE').body.expected_digest, current);
});

test('Unicode passwords do not remain in JavaScript bearer headers', async () => {
  const h = harness();
  await h.run('loadData()');
  h.context.sessionStorage.novelMemoryToken = 'Old credential';
  h.node('inputNewToken').value = '虚构测试管理密码';
  await h.node('btnChangePassword').onclick();
  assert.equal(h.context.sessionStorage.novelMemoryToken, undefined);
  await h.run('loadData()');
  const last = h.server.requests.at(-1);
  assert.equal(last.headers.Authorization, undefined);
});

test('invalid scope leaves the catalog available and visibly requires repair', async () => {
  const h = harness();
  h.server.invalidScope = true;
  await h.run('loadData()');
  assert.equal(h.node('libraryWarnings').hidden, false);
  assert.match(h.node('libraryWarnings').textContent, /配置异常/);
  assert.equal(h.node('mountCount').textContent, 0);
  h.node('btnSelectAll').onclick();
  await h.node('btnSaveMount').onclick();
  assert.equal(h.node('libraryWarnings').hidden, true);
});

test('dirty editor can cancel closing and warns before leaving', async () => {
  const h = harness();
  await h.run('loadData()');
  await h.run('openEditor("edit", "a_stage")');
  h.node('inputStageContent').value = 'Unsaved text';
  h.server.confirm = false;
  h.node('btnEditorClose').onclick();
  assert.equal(h.node('editorDrawer').classList.contains('open'), true);
  let prevented = false;
  h.events.get('beforeunload')({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
});

test('BOM and quoted JSON metadata fill the editor faithfully', async () => {
  const h = harness();
  h.context.input = '\ufeff---\nid: bom\ndate_range: day\ntitle: "\\"Quoted title\\""\ntags: tag\n---\n\nFictional text';
  h.run('parseAndFillForm("bom.md", input)');
  assert.equal(h.node('inputMemoryId').value, 'bom');
  assert.equal(h.node('inputStageTitle').value, '"Quoted title"');
  assert.equal(h.node('inputStageContent').value, 'Fictional text');
});
