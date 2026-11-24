#!/usr/bin/env node
/**
 * v1.7.2 扫描异步化 —— 前端（PAGE）jsdom 对抗测试（QA 自建，独立于工程师用例）。
 *
 * 重点：
 *  1. 真·nginx 504 HTML（经真实网关桩 + 真实 server.py 端到端）→ 必须是人话提示，
 *     绝不能出现 `Unexpected token '<'`；
 *  2. 分页收尾：扫描"结束"那一刻客户端落后于 total 时，尾页会不会被丢掉（漏条）；
 *  3. 扫描中按钮/取消/进度；连点不叠加；增量 append 不重复；扫描中改参数不错乱。
 *
 * 用法：node qa_frontend_scan.js [S1 S3 ...]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const http = require('http');
const { spawn, execFileSync } = require('child_process');
const { JSDOM, VirtualConsole } = require('jsdom');

const HERE = __dirname;
const ROOT = path.resolve(HERE, '..', '..');
const APP = path.join(ROOT, 'caesium', 'app');

let passed = 0;
const failures = [];
const only = process.argv.slice(2).map(function (x) { return x.toUpperCase(); });

function ok(name) { passed += 1; console.log('PASS ' + name); }
function fail(name, err) {
  failures.push([name, err && err.stack ? err.stack : String(err)]);
  console.log('FAIL ' + name);
  if (err) console.log('     ' + String(err.message || err).split('\n')[0]);
}
function test(name, fn) {
  const id = name.split(' ')[0];
  if (only.length && only.indexOf(String(id).toUpperCase()) < 0) return;
  return Promise.resolve().then(fn).then(function () { ok(name); }, function (e) { fail(name, e); });
}
function assert(c, m) { if (!c) throw new Error('断言失败: ' + m); }
function eq(a, b, m) {
  if (a !== b) throw new Error((m || '值不匹配') + ' 期望 ' + JSON.stringify(b) + '，实际 ' + JSON.stringify(a));
}
function contains(h, n, m) {
  if (String(h).indexOf(n) < 0) throw new Error((m || '文本') + ' 期望包含 ' + JSON.stringify(n) + '，实际 ' + JSON.stringify(h));
}
function notContains(h, n, m) {
  if (String(h).indexOf(n) >= 0) throw new Error((m || '文本') + ' 不应包含 ' + JSON.stringify(n) + '，实际 ' + JSON.stringify(h));
}

function loadPage() {
  const code = 'import sys; sys.path.insert(0, ' + JSON.stringify(APP) + ');' +
    'import server; sys.stdout.write(server.PAGE)';
  return execFileSync('python', ['-c', code], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
}
const PAGE = loadPage();

// 与 qa_gw_stub.py 里的 NGINX_504 完全一致（真实 nginx 504 页面）
const NGINX_504 = '<html>\r\n<head><title>504 Gateway Time-out</title></head>\r\n' +
  '<body>\r\n<center><h1>504 Gateway Time-out</h1></center>\r\n' +
  '<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n';

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 60); }); }

function makeDom(routes, opt) {
  const calls = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', function () {});
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:8390/app/caesium/',
    virtualConsole: virtualConsole,
    beforeParse: function (window) {
      window.alert = function () { window.__alertCalled = true; };
      window.confirm = function () { return true; };
      window.__alertCalled = false;
      window.fetch = function (input, init) {
        const full = String(input);
        const url = full.replace(/^\.?\//, '');
        const method = ((init && init.method) || 'GET').toUpperCase();
        const key = method + ' ' + url.split('?')[0];
        calls.push({ key: key, url: url, method: method, body: init && init.body });
        const handler = routes[key];
        let payload;
        if (typeof handler === 'function') payload = handler(init, url);
        else if (handler !== undefined) payload = handler;
        else payload = { ok: true };
        if (payload && payload.__raw !== undefined) {
          const raw = payload.__raw;
          return Promise.resolve({
            ok: false, status: payload.__status || 504,
            json: function () { return Promise.reject(new SyntaxError("Unexpected token '<'")); },
            text: function () { return Promise.resolve(raw); }
          });
        }
        if (payload && payload.__promise) return payload.__promise;
        return Promise.resolve({
          ok: true, status: 200,
          json: function () { return Promise.resolve(payload); },
          text: function () { return Promise.resolve(JSON.stringify(payload)); }
        });
      };
      if (opt && opt.onParse) opt.onParse(window);
    }
  });
  return { dom: dom, win: dom.window, doc: dom.window.document, calls: calls };
}

function rows(doc) { return Array.prototype.slice.call(doc.querySelectorAll('#tbody tr')); }
function names(doc) {
  return rows(doc).map(function (tr) { return tr.children[1].textContent.trim(); });
}

/** 造一批扫描结果条目 */
function files(start, n, prefix) {
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const k = start + i;
    out.push({ path: '/s/' + (prefix || 'f') + k + '.jpg', name: (prefix || 'f') + k + '.jpg',
      size: 1000 + k, ext: '.jpg', root: '/s', w: 100, h: 50 });
  }
  return out;
}

function baseRoutes(extra) {
  const r = {
    'GET api/status': { clt: true, version: '1.7.2' },
    'GET api/fs/roots': { roots: [{ path: '/s' }] },
    'GET api/fs/list': { dirs: [{ path: '/s/sub', name: 'sub' }], files: [] },
    'GET api/queue': { files: [] },
    'POST api/results/status': { done: {} },
    'GET api/tasks/active': { task: null },
    'POST api/queue': { ok: true }
  };
  return Object.assign(r, extra || {});
}

/* 走真实 UI 路径触发扫描：点「添加文件夹」→ 弹层里选一行 → 确定 */
async function addFolderViaUI(env) {
  const doc = env.doc;
  const btn = doc.getElementById('addFolderBtn');
  btn.disabled = false;
  btn.click();
  await tick(150);
  let row = doc.querySelector('#browserBody .row');
  if (!row) throw new Error('文件夹弹层没有「已授权文件夹」行，无法触发扫描');
  row.click();
  await tick(120);
  doc.getElementById('browserOK').click();
  await tick(120);
}

/* ------------------------------------------------------------------ 端到端：真网关 + 真 server.py */
function waitPort(port, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 30000);
  return new Promise(function (resolve, reject) {
    function probe() {
      const req = http.request({ host: '127.0.0.1', port: port, path: '/api/status', method: 'GET' }, function (res) {
        res.resume();
        resolve(true);
      });
      req.on('error', function () {
        if (Date.now() > deadline) reject(new Error('端口 ' + port + ' 未就绪'));
        else setTimeout(probe, 150);
      });
      req.end();
    }
    probe();
  });
}

function freePortSync() {
  const net = require('net');
  const srv = net.createServer();
  return new Promise(function (resolve) {
    srv.listen(0, '127.0.0.1', function () {
      const p = srv.address().port;
      srv.close(function () { resolve(p); });
    });
  });
}

function realFetch(base, opt) {
  const delay = (opt && opt.delayMs) || 0;
  return function (input, init) {
    const url = new URL(String(input).replace(/^\.?\//, ''), base + '/');
    const method = ((init && init.method) || 'GET').toUpperCase();
    const body = init && init.body ? String(init.body) : null;
    return new Promise(function (resolve, reject) {
      const send = function () {
      const req = http.request({
        host: url.hostname, port: url.port, path: url.pathname + url.search, method: method,
        headers: body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {}
      }, function (res) {
        const chunks = [];
        res.on('data', function (c) { chunks.push(c); });
        res.on('end', function () {
          const text = Buffer.concat(chunks).toString('utf8');
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
            text: function () { return Promise.resolve(text); },
            json: function () { return Promise.resolve(JSON.parse(text)); }
          });
        });
      });
      req.on('error', reject);
      if (body) req.write(body);
      req.end();
      };
      if (delay > 0) setTimeout(send, delay); else send();
    });
  };
}

async function scenarioRealGateway() {
  const tmp = fs.mkdtempSync(path.join(require('os').tmpdir(), 'qa_fe_'));
  const srvPort = await freePortSync();
  const gwPort = await freePortSync();
  const work = path.join(tmp, 'work');
  fs.mkdirSync(work, { recursive: true });
  // 让 /api/fs/roots 有内容，弹层里才有「已授权文件夹」行可选
  const accessible = path.join(tmp, 'pics');
  fs.mkdirSync(path.join(accessible, 'sub'), { recursive: true });
  fs.writeFileSync(path.join(accessible, 'sub', 'x.png'),
    Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex'));
  const env = Object.assign({}, process.env,
    { PYTHONIOENCODING: 'utf-8', CAESIUM_ACCESSIBLE_PATHS: accessible });
  const server = spawn('python', [path.join(APP, 'server.py'), '--port', String(srvPort), '--workdir', work],
    { cwd: APP, env: env, stdio: 'ignore' });
  let gw = null;
  try {
    await waitPort(srvPort);
    // 网关桩：强制 /api/scan 返回真实 nginx 504 HTML（模拟"上游其实在跑，但网关等不到"）
    gw = spawn('python', [path.join(HERE, 'qa_gw_stub.py'), '--listen', String(gwPort),
      '--upstream', '127.0.0.1:' + srvPort, '--timeout', '1.0', '--force-504', '/api/scan'],
      { cwd: HERE, env: env, stdio: 'ignore' });
    await waitPort(gwPort);
    return { server: server, gw: gw, srvPort: srvPort, gwPort: gwPort, tmp: tmp };
  } catch (e) {
    try { server.kill(); } catch (x) {}
    if (gw) { try { gw.kill(); } catch (x) {} }
    throw e;
  }
}

async function main() {
  console.log('== 前端扫描对抗测试 page=%d bytes\n', PAGE.length);

  // ---------------------------------------------------------------- S1 网关 504（桩注入真实 nginx 页面）
  await test('S1 「添加文件夹」时网关回 504 HTML → 人话提示，不出现 Unexpected token', async function () {
    const env = makeDom(baseRoutes({ 'POST api/scan': { __raw: NGINX_504, __status: 504 } }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(400);
    const txt = env.doc.getElementById('statusTxt').textContent;
    notContains(txt, 'Unexpected token', '状态栏');
    notContains(txt, '<html', '状态栏');
    contains(txt, '扫描失败', '状态栏');
    assert(/服务未响应|超时|异常内容|无法解析/.test(txt), '应给出人话原因，实际: ' + txt);
    eq(env.doc.getElementById('addFolderBtn').disabled, false, '失败后「添加文件夹」必须恢复可点');
    eq(env.doc.getElementById('scanCancelLink').classList.contains('hidden'), true,
      '失败后不应继续显示「取消扫描」');
    env.win.close();
  });

  // ---------------------------------------------------------------- S2 轮询中途 504
  await test('S2 扫描轮询中途网关 504 → 收尾、按钮恢复、不再继续轮询', async function () {
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's2', state: 'running' },
      'GET api/scan/status': { __raw: NGINX_504, __status: 504 }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(700);
    const txt = env.doc.getElementById('statusTxt').textContent;
    notContains(txt, 'Unexpected token', '状态栏');
    contains(txt, '扫描失败', '状态栏');
    const before = env.calls.filter(function (c) { return c.key === 'GET api/scan/status'; }).length;
    await tick(1200);
    const after = env.calls.filter(function (c) { return c.key === 'GET api/scan/status'; }).length;
    eq(after, before, '失败后必须停止轮询（不能每 500ms 打网关）');
    eq(env.doc.getElementById('addFolderBtn').disabled, false, '按钮恢复');
    env.win.close();
  });

  // ---------------------------------------------------------------- S3 分页收尾漏条（核心）
  await test('S3 扫描"结束"那一帧客户端落后于 total → 尾页不许丢（漏条）', async function () {
    const TOTAL = 1500, PAGE_SIZE = 500;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's3', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        // 关键：这一帧状态已经是 done，但服务端还有 offset+PAGE_SIZE 之后的数据没被拉走
        return { id: 's3', state: 'done', total: TOTAL, scanned: TOTAL,
          files: files(off, PAGE_SIZE), next_offset: off + PAGE_SIZE, truncated: false };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(1500);
    const n = rows(env.doc).length;
    const txt = env.doc.getElementById('statusTxt').textContent;
    eq(n, TOTAL, '扫描结束时列表里应有全部 ' + TOTAL + ' 个文件（实际 ' + n + '，丢 ' + (TOTAL - n) + ' 个）');
    contains(txt, '1500', '状态栏汇总应体现真实条数');
    env.win.close();
  });

  // ---------------------------------------------------------------- S4 连点不叠加
  await test('S4 扫描进行中再点「添加」（连点）→ 不再发第二次 /api/scan', async function () {
    let statusCalls = 0;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's4', state: 'running' },
      'GET api/scan/status': function (init, url) {
        statusCalls += 1;
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        const done = statusCalls > 3;
        return { id: 's4', state: done ? 'done' : 'running', total: done ? 100 : 50,
          scanned: 50, files: files(off, 50), next_offset: off + 50, truncated: false };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(120);
    const btn = env.doc.getElementById('addFolderBtn');
    for (let i = 0; i < 4; i += 1) { btn.disabled = false; btn.click(); }
    await tick(1400);
    const posts = env.calls.filter(function (c) { return c.key === 'POST api/scan'; }).length;
    eq(posts, 1, '扫描中再点必须被挡住（实际发了 ' + posts + ' 次 POST api/scan）');
    env.win.close();
  });

  // ---------------------------------------------------------------- S5 忙碌态
  await test('S5 扫描中：两个「添加」禁用 + 出现「取消扫描」；完成后恢复', async function () {
    const t0 = Date.now();
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's5', state: 'running' },
      'GET api/scan/status': function () {
        const done = (Date.now() - t0) > 1300;      // 让"扫描中"持续 1.3 秒，便于观察忙碌态
        return { id: 's5', state: done ? 'done' : 'running', total: 10, scanned: 10,
          files: done ? files(0, 10) : files(0, 5), next_offset: done ? 10 : 5, truncated: false };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(200);
    eq(env.doc.getElementById('addFileBtn').disabled, true, '扫描中「添加文件」应禁用');
    eq(env.doc.getElementById('addFolderBtn').disabled, true, '扫描中「添加文件夹」应禁用');
    eq(env.doc.getElementById('scanCancelLink').classList.contains('hidden'), false, '应出现「取消扫描」');
    await tick(1500);
    eq(env.doc.getElementById('addFileBtn').disabled, false, '完成后应恢复');
    eq(env.doc.getElementById('scanCancelLink').classList.contains('hidden'), true, '完成后「取消扫描」应收起');
    assert(rows(env.doc).length >= 10, '完成后应有 10 行，实际 ' + rows(env.doc).length);
    env.win.close();
  });

  // ---------------------------------------------------------------- S6 增量 append 去重
  await test('S6 两页含重复条目 → 列表不出现重复行', async function () {
    let n = 0;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's6', state: 'running' },
      'GET api/scan/status': function (init, url) {
        n += 1;
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        if (n === 1) return { id: 's6', state: 'running', total: 6, scanned: 6, files: files(0, 4), next_offset: 4 };
        // 第二页故意重叠：从 2 开始（正常不会发生，验证去重是兜底）
        return { id: 's6', state: 'done', total: 6, scanned: 6, files: files(2, 4), next_offset: 6 };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(1200);
    const ns = names(env.doc);
    const uniq = Array.from(new Set(ns));
    eq(ns.length, uniq.length, '不应有重复行：' + ns.join(','));
    env.win.close();
  });

  // ---------------------------------------------------------------- S7 扫描中改参数
  await test('S7 扫描中途改参数 / 切标签 → 列表与状态不错乱', async function () {
    let n = 0;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 's7', state: 'running' },
      'GET api/scan/status': function (init, url) {
        n += 1;
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        const done = n > 3;
        return { id: 's7', state: done ? 'done' : 'running', total: 40, scanned: 40,
          files: files(off, 10), next_offset: off + 10, truncated: false };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(200);
    const q = env.doc.getElementById('jpegQ');
    q.value = '55';
    q.dispatchEvent(new env.win.Event('input', { bubbles: true }));
    env.doc.querySelectorAll('.tab').forEach(function (t) { t.click(); });
    await tick(1600);
    const ns = names(env.doc);
    eq(Array.from(new Set(ns)).length, ns.length, '不应出现重复行');
    assert(!/扫描失败/.test(env.doc.getElementById('statusTxt').textContent), '不应显示扫描失败');
    assert(!env.doc.getElementById('addFileBtn').disabled, '完成后按钮应恢复');
    env.win.close();
  });

  // ---------------------------------------------------------------- S8 真网关端到端 504
  await test('S8 端到端：真 server.py + 真网关桩 504 HTML → 页面人话提示', async function () {
    const ctx = await scenarioRealGateway();
    try {
      const env = makeDom({}, { onParse: function (window) { window.fetch = realFetch('http://127.0.0.1:' + ctx.gwPort + '/app/caesium'); } });
      await tick(300);
      await addFolderViaUI(env);
      await tick(1500);
      const txt = env.doc.getElementById('statusTxt').textContent;
      notContains(txt, 'Unexpected token', '状态栏');
      contains(txt, '扫描失败', '状态栏');
      assert(/服务未响应|超时|异常内容|无法解析/.test(txt), '应给出人话原因，实际: ' + txt);
      env.win.close();
    } finally {
      try { ctx.server.kill(); } catch (e) {}
      try { ctx.gw.kill(); } catch (e) {}
    }
  });

  // ---------------------------------------------------------------- S9 端到端：真目录 + 真服务
  async function scanRealTree(total, delayMs, label) {
    const os = require('os');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'qa_scan_tree_'));
    const TOTAL = total;
    const png = Buffer.concat([
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      Buffer.from([0, 0, 0, 0x0d]), Buffer.from('IHDR', 'ascii'),
      Buffer.from([0, 0, 0x02, 0x80, 0, 0, 0x01, 0xe0]),
      Buffer.from([8, 2, 0, 0, 0]), Buffer.alloc(8),
      Buffer.from('IEND', 'ascii'), Buffer.from([0xae, 0x42, 0x60, 0x82])
    ]);
    for (let i = 0; i < 10; i += 1) { fs.mkdirSync(path.join(tmp, 'd' + i), { recursive: true }); }
    for (let i = 0; i < TOTAL; i += 1) {
      fs.writeFileSync(path.join(tmp, 'd' + (i % 10), 'p' + String(i).padStart(5, '0') + '.png'), png);
    }
    const srvPort = await freePortSync();
    const work = fs.mkdtempSync(path.join(os.tmpdir(), 'qa_scan_work_'));
    const env = Object.assign({}, process.env, {
      PYTHONIOENCODING: 'utf-8', CAESIUM_ACCESSIBLE_PATHS: tmp
    });
    const server = spawn('python', [path.join(APP, 'server.py'), '--port', String(srvPort), '--workdir', work],
      { cwd: APP, env: env, stdio: 'ignore' });
    try {
      await waitPort(srvPort);
      const env2 = makeDom({}, { onParse: function (window) {
        window.fetch = realFetch('http://127.0.0.1:' + srvPort + '/app/caesium', { delayMs: delayMs });
      } });
      await tick(400);
      const doc = env2.doc;
      const btn = doc.getElementById('addFolderBtn');
      btn.disabled = false;
      btn.click();
      await tick(500);
      const row = doc.querySelector('#browserBody .row');
      assert(row, '应列出已授权文件夹（CAESIUM_ACCESSIBLE_PATHS=' + tmp + '）');
      row.click();
      await tick(400);
      doc.getElementById('browserOK').click();
      // 等扫描收尾（状态栏出现汇总或失败）
      let txt = '';
      const deadline = Date.now() + 60000;
      while (Date.now() < deadline) {
        await tick(300);
        txt = doc.getElementById('statusTxt').textContent;
        if (/添加 \d+ 个文件|扫描失败|已取消扫描/.test(txt)) break;
      }
      const n = rows(doc).length;
      const m = /添加 (\d+) 个文件/.exec(txt);
      console.log('     [实测] ' + label + ' 状态栏: ' + txt);
      console.log('     [实测] ' + label + ' 列表行数=' + n + ' / 目录里真实文件数=' + TOTAL);
      eq(n, TOTAL, label + ' 扫描结束后列表必须有全部 ' + TOTAL + ' 个（实际 ' + n + '，丢 ' + (TOTAL - n) + ' 个）');
      if (m) eq(parseInt(m[1], 10), TOTAL, '状态栏汇总的"添加 N 个"也应等于 ' + TOTAL);
      env2.win.close();
    } finally {
      try { server.kill(); } catch (e) {}
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
      try { fs.rmSync(work, { recursive: true, force: true }); } catch (e) {}
    }
  }

  await test('S9 端到端：真 server.py + 真 5000 张图（本地直连），列表必须收齐', async function () {
    await scanRealTree(5000, 0, '本地');
  });

  await test('S9b 端到端：同 5000 张图，但模拟 300ms RTT（远程/弱网访问 NAS）', async function () {
    await scanRealTree(5000, 300, '300ms RTT');
  });

  // ---------------------------------------------------------------- S10 truncated 语义双检（QA 第 2 轮新增）
  await test('S10a truncated=true 时不得误报「结果可能不完整」', async function () {
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 'sa', state: 'running' },
      'GET api/scan/status': { id: 'sa', state: 'done', total: 1500, scanned: 50000,
        truncated: true, files: files(0, 500), next_offset: 500 }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(900);
    const txt = env.doc.getElementById('statusTxt').textContent;
    notContains(txt, '结果可能不完整', 'truncated 是服务端预期上限，不该误报不完整');
    contains(txt, '上限', '应提示到达上限');
    env.win.close();
  });

  await test('S10b truncated=true 且客户端还落后时，已收录的 total 仍要全部拉到', async function () {
    const TOTAL = 1500, PAGE_SIZE = 500;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 'sb', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        return { id: 'sb', state: 'done', total: TOTAL, scanned: TOTAL, truncated: true,
          files: files(off, PAGE_SIZE), next_offset: off + PAGE_SIZE };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(1500);
    const n = rows(env.doc).length;
    const txt = env.doc.getElementById('statusTxt').textContent;
    console.log('     [实测] S10b 列表行数=' + n + ' / 已收录 total=' + TOTAL + ' 状态栏=' + txt);
    eq(n, TOTAL, 'truncated 只代表"目录没扫全"，已收录的 ' + TOTAL + ' 条不许丢（实际 ' + n + '）');
    env.win.close();
  });

  // ---------------------------------------------------------------- S11 弱网 700ms > tick 500ms：轮询互斥
  await test('S11 响应 700ms（>SCAN_TICK 500ms）：在飞 status 请求 ≤1 且 offset 零重复', async function () {
    let inflight = 0, maxInflight = 0;
    const offsets = [];
    let calls = 0;
    const env = makeDom(baseRoutes({
      'POST api/scan': { scan_id: 'sc', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        offsets.push(off);
        inflight += 1; maxInflight = Math.max(maxInflight, inflight);
        calls += 1;
        const done = calls >= 4;
        return { __promise: new Promise(function (resolve) {
          setTimeout(function () {
            inflight -= 1;
            const payload = { id: 'sc', state: done ? 'done' : 'running', total: done ? 2000 : 1000,
              scanned: 1000, truncated: false, files: files(off, 500), next_offset: off + 500 };
            resolve({ ok: true, status: 200,
              text: function () { return Promise.resolve(JSON.stringify(payload)); },
              json: function () { return Promise.resolve(payload); } });
          }, 700);
        }) };
      }
    }));
    await tick(150);
    await addFolderViaUI(env);
    await tick(6000);
    const dup = offsets.length - Array.from(new Set(offsets)).length;
    console.log('     [实测] S11 请求 offset 序列=' + offsets.join(',') + ' 最大并发=' + maxInflight + ' 重复=' + dup);
    assert(maxInflight <= 1, '同一时刻在飞的 status 请求必须 ≤1，实测峰值 ' + maxInflight);
    eq(dup, 0, 'offset 不允许重复拉（实测重复 ' + dup + ' 次）');
    env.win.close();
  });

  console.log('\n== ' + passed + ' passed, ' + failures.length + ' failed ==');
  failures.forEach(function (f) { console.log('\n---- ' + f[0] + ' ----\n' + f[1]); });
  process.exit(failures.length ? 1 : 0);
}

main().catch(function (e) { console.error(e); process.exit(2); });
