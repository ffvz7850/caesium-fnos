#!/usr/bin/env node
/**
 * 前端（PAGE 内嵌脚本）DOM 断言测试。
 *
 * 从 server.py 里取出 PAGE，用 jsdom 真解析 + 真执行内联 script，把 fetch 打成桩，
 * 验证：状态列三种文案、按钮文案切换、改参数清空完成状态、「清空」走自绘确认弹层、
 * 输出到原文件夹时「已存在文件」下拉置灰、页面加载接回进行中的任务。
 *
 * 用法： node test_frontend.js
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { JSDOM, VirtualConsole } = require('jsdom');

const HERE = __dirname;
const APP = path.join(HERE, '..', 'caesium', 'app');

let passed = 0;
const failures = [];

function ok(name) {
  passed += 1;
  console.log('PASS ' + name);
}

function fail(name, err) {
  failures.push([name, err && err.stack ? err.stack : String(err)]);
  console.log('FAIL ' + name);
}

function test(name, fn) {
  try {
    fn();
    ok(name);
  } catch (e) {
    fail(name, e);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error('断言失败: ' + msg);
}

function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error((msg || '值不匹配') + ' 期望 ' + JSON.stringify(expected) + '，实际 ' + JSON.stringify(actual));
  }
}

function contains(hay, needle, msg) {
  if (String(hay).indexOf(needle) < 0) {
    throw new Error((msg || '文本') + ' 期望包含 ' + JSON.stringify(needle) + '，实际 ' + JSON.stringify(hay));
  }
}

/** 从 server.py 取出 PAGE 常量（真源码，不是复制品） */
function loadPage() {
  const code =
    'import sys; sys.path.insert(0, ' + JSON.stringify(APP) + ');' +
    'import server; sys.stdout.write(server.PAGE)';
  return execFileSync('python', ['-c', code], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
}

const PAGE = loadPage();

function tick(ms) {
  return new Promise(function (r) { setTimeout(r, ms || 60); });
}

/**
 * 建一个 jsdom 环境。
 * @param {object} routes  路由桩：key 形如 'GET api/queue' / 'POST api/compress'
 * @param {object} opt     {onParse: (window)=>void}
 */
function makeDom(routes, opt) {
  const calls = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', function () { /* 图片加载失败之类，忽略 */ });
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:8390/app/caesium/',
    virtualConsole: virtualConsole,
    beforeParse: function (window) {
      window.alert = function () { window.__alertCalled = true; };
      window.confirm = function () { window.__confirmCalled = true; return true; };
      window.__confirmCalled = false;
      window.__alertCalled = false;
      window.fetch = function (input, init) {
        const url = String(input).replace(/^\.?\//, '');
        const method = ((init && init.method) || 'GET').toUpperCase();
        const key = method + ' ' + url.split('?')[0];
        calls.push({ key: key, url: url, body: init && init.body });
        const handler = routes[key];
        let payload;
        if (typeof handler === 'function') payload = handler(init, url);
        else if (handler !== undefined) payload = handler;
        else payload = { ok: true };
        // {__raw:'<html>…'} 模拟"网关返回 HTML 错误页"（非 JSON 响应）
        if (payload && payload.__raw !== undefined) {
          const raw = payload.__raw;
          const status = payload.__status || 504;
          return Promise.resolve({
            ok: false,
            status: status,
            json: function () { return Promise.reject(new SyntaxError("Unexpected token '<'")); },
            text: function () { return Promise.resolve(raw); }
          });
        }
        // {__delay:ms, __data:{...}} 模拟慢网络（用于验证"同一时刻只有 1 个请求在飞"）
        if (payload && payload.__delay) {
          const ms = payload.__delay, data = (payload.__data || {});
          window.__inflight = (window.__inflight || 0) + 1;
          window.__inflightMax = Math.max(window.__inflightMax || 0, window.__inflight);
          return new Promise(function (resolve) {
            setTimeout(function () {
              window.__inflight -= 1;
              resolve({
                ok: true, status: 200,
                json: function () { return Promise.resolve(data); },
                text: function () { return Promise.resolve(JSON.stringify(data)); }
              });
            }, ms);
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: function () { return Promise.resolve(payload); },
          text: function () { return Promise.resolve(JSON.stringify(payload)); }
        });
      };
      if (opt && opt.onParse) opt.onParse(window);
    }
  });
  return { dom: dom, win: dom.window, doc: dom.window.document, calls: calls };
}

function rows(doc) {
  return Array.prototype.slice.call(doc.querySelectorAll('#tbody tr'));
}

function statusTexts(doc) {
  return rows(doc).map(function (tr) { return tr.children[5].textContent.trim(); });
}

const FILE_QUEUE = {
  files: [
    { path: '/s/a.jpg', name: 'a.jpg', size: 1048576, ext: '.jpg', root: '/s', w: 100, h: 100 },
    { path: '/s/b.jpg', name: 'b.jpg', size: 2097152, ext: '.jpg', root: '/s', w: 200, h: 100 },
    { path: '/s/c.jpg', name: 'c.jpg', size: 3145728, ext: '.jpg', root: '/s', w: 300, h: 100 }
  ]
};

const DONE_STATUS = {
  done: {
    '/s/a.jpg': { status: 'ok', out: '/out/a.jpg', in_size: 1048576, out_size: 524288, sig: 'aaaa' },
    '/s/b.jpg': { status: 'skipped', out: '/out/b.jpg', in_size: 2097152, out_size: 1000000, sig: 'aaaa' }
  }
};

async function scenarioDoneStates() {
  const env = makeDom({
    'GET api/status': { clt: true, version: '1.7.1' },
    'GET api/fs/roots': { roots: [{ path: '/s' }] },
    'GET api/queue': FILE_QUEUE,
    'GET api/results/status2': {},
    'POST api/results/status': DONE_STATUS,
    'GET api/tasks/active': { task: null }
  });
  const doc = env.doc, win = env.win;
  await tick(200);
  return { env: env, doc: doc, win: win };
}

async function main() {
  // ---------------------------------------------------------------- 1 断点续传标记 + 按钮文案
  {
    const ctx = await scenarioDoneStates();
    const doc = ctx.doc;
    test('加载后按 results/status 把已完成文件标回（成功 / 已跳过 / 等待）', function () {
      eq(rows(doc).length, 3, '应渲染 3 行');
      eq(statusTexts(doc).join(','), '成功,已跳过,等待', '状态列文案');
      assert(doc.querySelector('#tbody tr:nth-child(1) .st-ok'), '第 1 行应有 .st-ok');
      assert(doc.querySelector('#tbody tr:nth-child(2) .st-skip'), '第 2 行应有 .st-skip');
    });

    test('表头统计含 成功 / 跳过 / 失败', function () {
      const txt = doc.getElementById('sumSize').textContent;
      contains(txt, '成功 1', '统计');
      contains(txt, '跳过 1', '统计');
      contains(txt, '失败 0', '统计');
      contains(txt, '合计', '统计');
    });

    test('主按钮文案在有已完成项时变成「继续压缩（已完成 N）」', function () {
      eq(doc.getElementById('startBtn').textContent, '继续压缩（已完成 2）', '按钮文案');
      eq(doc.getElementById('recompressAllBtn').disabled, false, '有已完成项时「全部重压」应可用');
    });

    test('改压缩参数后清空所有完成状态并还原按钮文案', function () {
      const q = doc.getElementById('jpegQ');
      q.value = '60';
      q.dispatchEvent(new ctx.win.Event('input', { bubbles: true }));
      eq(statusTexts(doc).join(','), '等待,等待,等待', '改参数后状态应全部清空');
      eq(doc.getElementById('startBtn').textContent, '压缩', '按钮文案应还原');
      eq(doc.getElementById('recompressAllBtn').disabled, true, '无已完成项时「全部重压」应禁用');
    });

    ctx.win.close();
  }

  // ---------------------------------------------------------------- 2 三种状态 + 接回在跑的任务
  {
    const activeTask = {
      task: {
        id: 'abcdef123456', state: 'running', total: 3, done: 3, ok: 1, skipped: 1, fail: 1,
        output: '/out', keep_structure: false, error: null, elapsed: 5,
        results: [
          { path: '/s/a.jpg', status: 'ok', out: '/out/a.jpg', in_size: 1048576, out_size: 524288 },
          { path: '/s/b.jpg', status: 'skipped', out: '/out/b.jpg', in_size: 2097152, out_size: 1000000 },
          { path: '/s/c.jpg', status: 'error', error: 'boom' }
        ]
      }
    };
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': activeTask,
      'GET api/tasks/abcdef123456': activeTask.task
    });
    const doc = env.doc;
    await tick(200);

    test('页面加载时接回进行中的任务：恢复锁定态 + 进度条 + 取消链接', function () {
      assert(doc.body.classList.contains('locked'), '应处于锁定态');
      assert(doc.getElementById('barWrap').style.display === 'block', '进度条应显示');
      assert(!doc.getElementById('cancelLink').classList.contains('hidden'), '取消链接应显示');
      contains(doc.getElementById('statusTxt').textContent, '接回进行中的任务', '状态栏');
      const lockable = ['addFileBtn', 'startBtn', 'mode', 'outPath', 'existPolicy'];
      lockable.forEach(function (id) {
        eq(doc.getElementById(id).disabled, true, id + ' 应被锁定');
      });
    });

    test('任务结果三种状态分别渲染为 成功 / 已跳过 / 失败', function () {
      eq(statusTexts(doc).join(','), '成功,已跳过,失败', '状态列文案');
      assert(doc.querySelector('#tbody tr:nth-child(3) .st-err'), '第 3 行应有 .st-err');
    });
    env.win.close();
  }

  // ---------------------------------------------------------------- 3 清空按钮走自绘确认
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true }
    });
    const doc = env.doc, win = env.win;
    await tick(200);

    test('「清空」不调用原生 confirm，而是弹出自绘确认层', function () {
      doc.getElementById('clearBtn').click();
      eq(win.__confirmCalled, false, '不允许调用原生 confirm（飞牛 iframe 里会被 sandbox 拦掉）');
      assert(doc.getElementById('confirmMask').classList.contains('show'), '自绘确认层应显示');
      contains(doc.getElementById('confirmText').textContent, '清空全部 3 个文件', '确认文案');
      eq(rows(doc).length, 3, '确认前不能真的清空');
    });

    test('确认层「取消」不清空，「确定」才清空', function () {
      doc.getElementById('confirmCancel').click();
      assert(!doc.getElementById('confirmMask').classList.contains('show'), '取消后确认层应关闭');
      eq(rows(doc).length, 3, '取消后文件应保留');
      doc.getElementById('clearBtn').click();
      doc.getElementById('confirmOK').click();
      eq(rows(doc).length, 0, '确定后应清空');
      assert(!doc.getElementById('empty').classList.contains('hidden'), '应显示空列表提示');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 4 已存在文件下拉
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null }
    });
    const doc = env.doc, win = env.win;
    await tick(150);

    test('「已存在文件」下拉默认跳过，三态齐全', function () {
      const sel = doc.getElementById('existPolicy');
      assert(sel, '必须存在 #existPolicy');
      eq(sel.value, 'skip', '默认应为跳过');
      eq(Array.prototype.map.call(sel.options, function (o) { return o.value; }).join(','),
        'skip,overwrite,rename', '三个策略');
      contains(doc.getElementById('existPolicyDesc').textContent, '跳过', '说明文案');
    });

    test('勾选「输出到原文件夹」时下拉置灰并给出提示', function () {
      doc.getElementById('sameFolder').click();
      eq(doc.getElementById('existPolicy').disabled, true, '应置灰');
      contains(doc.getElementById('existPolicyDesc').textContent, '只能覆盖', '提示文案');
      win.__confirmCalled = false;
      doc.getElementById('sameFolder').click();
      eq(doc.getElementById('existPolicy').disabled, false, '取消勾选后应恢复');
    });

    test('「覆盖原图」的安全提示必须走自绘确认层（原生 alert 会被 sandbox 拦掉）', function () {
      const box = doc.getElementById('sameFolder');
      win.__alertCalled = false;
      box.click();                                  // 勾选 → 必须弹自绘确认
      eq(win.__alertCalled, false, '不允许用原生 alert（飞牛 iframe 里会被静默拦掉）');
      assert(doc.getElementById('confirmMask').classList.contains('show'), '应弹出确认层');
      contains(doc.getElementById('confirmText').textContent, '覆盖原图', '确认文案应说明会覆盖原图');
      doc.getElementById('confirmCancel').click();   // 取消 → 回退勾选
      eq(box.checked, false, '取消后必须回退勾选');
      assert(!doc.getElementById('confirmMask').classList.contains('show'), '取消后确认层应关闭');
      eq(doc.getElementById('existPolicy').disabled, false, '回退后「已存在文件」下拉应恢复可用');
      contains(doc.getElementById('statusTxt').textContent, '已取消', '状态栏应说明已取消');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 5 提交时带上 exist_policy
  {
    let compressBody = null;
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/compress': function (init) {
        compressBody = JSON.parse(init.body);
        return { task_id: 'task123456' };
      },
      'GET api/tasks/task123456': {
        id: 'task123456', state: 'done', total: 3, done: 3, ok: 3, skipped: 0, fail: 0,
        output: '/out', keep_structure: true, elapsed: 1, results: []
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    doc.getElementById('outPath').value = '/out';
    doc.getElementById('outPath').dispatchEvent(new win.Event('change', { bubbles: true }));
    doc.getElementById('existPolicy').value = 'rename';
    eq(doc.getElementById('startBtn').disabled, false, '选了输出目录后主按钮应可用');
    doc.getElementById('startBtn').click();
    await tick(150);

    test('提交任务时把 exist_policy / force_recompress 传给后端', function () {
      assert(compressBody, '应发出 /api/compress 请求');
      eq(compressBody.options.exist_policy, 'rename', 'exist_policy 应带上');
      eq(compressBody.force_recompress, false, '普通压缩不该 force');
      eq(compressBody.files.length, 3, '应提交全部文件（后端自己跳过，保证 sig 判断唯一真理）');
    });

    win.close();
  }

  // ---------------------------------------------------------------- 6 全部重压
  {
    const bodies = [];
    let cleared = false;
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': DONE_STATUS,
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/results/clear': function () { cleared = true; return { ok: true, count: 0 }; },
      'POST api/compress': function (init) {
        bodies.push(JSON.parse(init.body));
        return { task_id: 'force123456' };
      },
      'GET api/tasks/force123456': {
        id: 'force123456', state: 'done', total: 3, done: 3, ok: 3, skipped: 0, fail: 0,
        output: '/out', keep_structure: true, elapsed: 1, results: []
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    doc.getElementById('outPath').value = '/out';
    doc.getElementById('outPath').dispatchEvent(new win.Event('change', { bubbles: true }));
    doc.getElementById('recompressAllBtn').click();
    await tick(200);

    test('「全部重压」= 先 POST /api/results/clear 再全量 force_recompress 提交', function () {
      eq(cleared, true, '应先清空 results');
      eq(bodies.length, 1, '应提交一次任务');
      eq(bodies[0].force_recompress, true, 'force_recompress 必须为 true');
      eq(bodies[0].files.length, 3, '应全量提交');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 7 状态栏统计含跳过
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.1' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/compress': { task_id: 'poll123456' },
      'GET api/tasks/poll123456': {
        id: 'poll123456', state: 'done', total: 3, done: 3, ok: 1, skipped: 1, fail: 1,
        output: '/out', keep_structure: true, elapsed: 2,
        results: [
          { path: '/s/a.jpg', status: 'ok', out: '/out/a.jpg', in_size: 1048576, out_size: 400000 },
          { path: '/s/b.jpg', status: 'skipped', out: '/out/b.jpg' },
          { path: '/s/c.jpg', status: 'error', error: 'boom' }
        ]
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    doc.getElementById('outPath').value = '/out';
    doc.getElementById('outPath').dispatchEvent(new win.Event('change', { bubbles: true }));
    doc.getElementById('startBtn').click();
    await tick(1500);   // poll 间隔 1200ms

    test('任务结束后状态栏区分 成功 / 跳过 / 失败', function () {
      const txt = doc.getElementById('statusTxt').textContent;
      contains(txt, '成功 1', '状态栏');
      contains(txt, '跳过 1', '状态栏');
      contains(txt, '失败 1', '状态栏');
      eq(statusTexts(doc).join(','), '成功,已跳过,失败', '三态文案');
      assert(doc.getElementById('logLink') && !doc.getElementById('logLink').classList.contains('hidden'),
        '应能查看日志');
      contains(doc.getElementById('logBody').textContent, '(已跳过)', '日志应标注已跳过');
      eq(doc.body.classList.contains('locked'), false, '任务结束后应解锁');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 8 大目录扫描（异步 + 分页增量）
  /* 走真实用户路径：点「添加文件夹」→ 浏览器弹层选一个已授权文件夹 → 点「选择此文件夹」
     → browserPick → addPaths（jsdom 里没有飞牛 SDK，pickNativeDir() 会回落到自建弹层） */
  async function addFolderViaUI(env) {
    const doc = env.doc;
    doc.getElementById('addFolderBtn').click();
    await tick(100);
    const row = doc.querySelector('#browserBody .row');
    assert(row, '文件夹弹层应渲染出「已授权文件夹」行');
    row.click();
    await tick(100);
    doc.getElementById('browserOK').click();
    await tick(80);
  }

  {
    let scanDone = false;
    let scanPosts = [];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': function (init) {
        scanPosts.push(JSON.parse(init.body));
        return { scan_id: 'sid1', state: 'running' };
      },
      'GET api/scan/status': function (init, url) {
        const q = {};
        url.split('?')[1].split('&').forEach(function (kv) {
          const p = kv.split('='); q[p[0]] = decodeURIComponent(p[1] || '');
        });
        const off = parseInt(q.offset, 10) || 0;
        const f = function (p, name, size) {
          return { path: p, name: name, size: size, ext: '.jpg', root: '/s', w: 10, h: 10 };
        };
        if (!scanDone) {
          return { id: 'sid1', state: 'running', scanned: 30, total: 2, truncated: false,
                   offset: 0, next_offset: 2, files: [f('/s/a.jpg', 'a.jpg', 100), f('/s/b.jpg', 'b.jpg', 200)] };
        }
        // 收尾那一页故意重复带上 a.jpg（分页边界/并发追加时真的会出现）→ 必须去重
        return { id: 'sid1', state: 'done', scanned: 40, total: 3, truncated: false,
                 offset: off, next_offset: off + 2, files: [f('/s/c.jpg', 'c.jpg', 300), f('/s/a.jpg', 'a.jpg', 100)] };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);

    await addFolderViaUI(env);

    test('扫描中：两个「添加」按钮禁用 + 状态栏显示已发现数 + 出现「取消扫描」', function () {
      eq(scanPosts.length, 1, '应 POST 一次 api/scan');
      eq(JSON.stringify(scanPosts[0].paths), JSON.stringify(['/s']), '应把选中的目录交给后端');
      eq(doc.getElementById('addFolderBtn').disabled, true, '扫描中「添加文件夹」必须禁用（避免叠加）');
      eq(doc.getElementById('addFileBtn').disabled, true, '扫描中「添加文件」也必须禁用');
      eq(doc.getElementById('scanCancelLink').classList.contains('hidden'), false, '应出现「取消扫描」');
      contains(doc.getElementById('statusTxt').textContent, '正在扫描', '状态栏应显示扫描中');
      contains(doc.getElementById('statusTxt').textContent, '已发现 2 个', '状态栏应显示已发现数');
      eq(rows(doc).length, 2, '第一页应已 append 进列表（增量，不等扫描结束）');
    });

    scanDone = true;
    await tick(700);      // 等下一个 500ms tick

    test('扫描完成：增量 append 不重复，按钮恢复，状态栏给汇总', function () {
      eq(rows(doc).length, 3, 'a/b/c 共 3 行（重复的 a.jpg 不许再插一行）');
      const names = rows(doc).map(function (tr) { return tr.querySelector('.p').textContent.trim(); });
      eq(names.join(','), 'a.jpg,b.jpg,c.jpg', '顺序应为增量追加');
      const st = doc.getElementById('statusTxt').textContent;
      contains(st, '添加 3 个文件', '两次增量共新增 a/b/c 三个');
      contains(st, '已在列表中', '重复项应说明是"已在列表中"');
      contains(st, '未重复添加', '应说明没有把文件丢掉');
      contains(st, '清空', '应告知如何全部重加');
      eq(st.indexOf('跳过 1 个重复'), -1, '不许再用容易误解的「跳过 N 个重复」措辞');
      eq(doc.getElementById('addFolderBtn').disabled, false, '扫描结束后按钮应恢复');
      eq(doc.getElementById('scanCancelLink').classList.contains('hidden'), true, '「取消扫描」应隐藏');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 9 扫描取消
  {
    let canceled = false;
    const cancelBodies = [];
    let statusPolls = 0;
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'sid2', state: 'running' },
      'POST api/scan/cancel': function (init) {
        cancelBodies.push(JSON.parse(init.body));
        canceled = true;
        return { ok: true, scan_id: 'sid2', state: 'canceled' };
      },
      'GET api/scan/status': function () {
        statusPolls += 1;
        if (!canceled) {
          return { id: 'sid2', state: 'running', scanned: 5, total: 1, truncated: false,
                   offset: 0, next_offset: 1, files: [{ path: '/s/x.jpg', name: 'x.jpg', size: 1, ext: '.jpg', root: '/s' }] };
        }
        return { id: 'sid2', state: 'canceled', scanned: 5, total: 1, truncated: false,
                 offset: 0, next_offset: 1, files: [] };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);

    doc.getElementById('scanCancelLink').click();
    await tick(30);
    const cancelSnapshot = cancelBodies.slice();
    const cancelText = doc.getElementById('statusTxt').textContent;

    test('扫描中「取消扫描」发对请求（必须带 scan_id）', function () {
      eq(cancelSnapshot.length, 1, '应 POST 一次 api/scan/cancel');
      eq(cancelSnapshot[0].scan_id, 'sid2', '必须带上 scan_id');
      contains(cancelText, '正在取消', '状态栏应提示正在取消');
    });

    await tick(700);
    const pollsAfterFinish = statusPolls;

    test('取消后：state=canceled、按钮恢复、「取消扫描」隐藏', function () {
      contains(doc.getElementById('statusTxt').textContent, '已取消扫描', '状态栏应说明已取消');
      eq(doc.getElementById('addFolderBtn').disabled, false, '取消后按钮应恢复');
      eq(doc.getElementById('scanCancelLink').classList.contains('hidden'), true, '「取消扫描」应隐藏');
    });

    await tick(600);
    test('取消收尾后不再继续轮询 status', function () {
      eq(statusPolls, pollsAfterFinish, '收尾之后不该再发 status 请求');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 10 网关返回 HTML（非 JSON）
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      // 飞牛网关等不到响应时的真实返回
      'POST api/scan': {
        __raw: '<html>\n<head><title>504 Gateway Time-out</title></head>\n' +
               '<body><center><h1>504 Gateway Time-out</h1></center><hr><center>nginx</center></body>\n</html>\n'
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(100);

    test('非 JSON 响应（网关 504 HTML）→ 人话提示，不再抛 Unexpected token', function () {
      const st = doc.getElementById('statusTxt').textContent;
      contains(st, '服务未响应或超时', '应提示服务未响应/超时');
      eq(st.indexOf('Unexpected token'), -1, '不许把 JSON 解析异常原样丢给用户');
      eq(st.indexOf('<html'), -1, '不许把 HTML 片段丢给用户');
      eq(doc.getElementById('addFolderBtn').disabled, false, '失败后按钮必须恢复（允许重试）');
      eq(doc.getElementById('scanCancelLink').classList.contains('hidden'), true, '失败后不该显示取消');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 11 分页收尾不许丢尾页（P0）
  {
    const TOTAL = 1500, PAGE = 500;
    const seenOffsets = [];
    const f = function (off, n) {
      const out = [];
      for (let i = 0; i < n; i += 1) {
        const k = off + i;
        out.push({ path: '/s/k' + k + '.jpg', name: 'k' + k + '.jpg', size: 10, ext: '.jpg', root: '/s', w: 1, h: 1 });
      }
      return out;
    };
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'p0', state: 'running' },
      // 关键：每一帧服务端都说 done，但一帧只给 500 条 —— 收尾必须先把 1500 条拉完
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        seenOffsets.push(off);
        return { id: 'p0', state: 'done', total: TOTAL, scanned: TOTAL,
                 files: f(off, PAGE), next_offset: off + PAGE, truncated: false };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(1600);

    test('收尾帧落后于 total 时不许提前收尾（1500 条一条不少）', function () {
      eq(rows(doc).length, TOTAL, '列表应有全部 1500 行，实际 ' + rows(doc).length);
      contains(doc.getElementById('statusTxt').textContent, '1500', '状态栏应体现真实条数');
      eq(Array.from(new Set(seenOffsets)).length, seenOffsets.length,
        '同一 offset 不许被拉两次，实际 ' + JSON.stringify(seenOffsets));
    });
    win.close();
  }

  // ---------------------------------------------------------------- 12 轮询互斥（弱网不叠加）
  {
    const offsets = [];
    const f = function (off, n) {
      const out = [];
      for (let i = 0; i < n; i += 1) {
        const k = off + i;
        out.push({ path: '/s/w' + k + '.jpg', name: 'w' + k + '.jpg', size: 10, ext: '.jpg', root: '/s' });
      }
      return out;
    };
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'mutex', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        offsets.push(off);
        // 响应比 tick（500ms）慢：上一轮没回来时不许再发
        return { __delay: 700, __data: { id: 'mutex', state: 'running', total: 4000, scanned: 4000,
                                         files: f(off, 500), next_offset: off + 500, truncated: false } };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(2600);

    test('弱网（响应 700ms > tick 500ms）时同一时刻只有 1 个 status 请求在飞', function () {
      assert(win.__inflightMax >= 1, '应确实发过请求');
      eq(win.__inflightMax, 1, '并发在飞请求数必须 ≤1，实际 ' + win.__inflightMax);
      eq(Array.from(new Set(offsets)).length, offsets.length,
        '同一 offset 不许重复拉，实际 ' + JSON.stringify(offsets));
      const posts = env.calls.filter(function (c) { return c.key === 'POST api/scan'; }).length;
      eq(posts, 1, '整个过程中只允许 1 次 POST api/scan');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 13 truncated 不误报"不完整"
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'trunc', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        const out = [];
        for (let i = 0; i < 500; i += 1) {
          out.push({ path: '/s/t' + i + '.jpg', name: 't' + i + '.jpg', size: 1, ext: '.jpg', root: '/s' });
        }
        // 真实服务端在 offset 超出已收录范围时会给空页、且 next_offset 不前进；
        // 这里如实模拟（避免桩数据造成"无限推进却永远没有文件"的假象）
        return { id: 'trunc', state: 'done', total: 50000, scanned: 50000,
                 files: off === 0 ? out : [], next_offset: off === 0 ? 500 : off, truncated: true };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(1200);

    test('truncated=true（到上限）时不得误报「结果可能不完整」', function () {
      const st = doc.getElementById('statusTxt').textContent;
      contains(st, '上限', '应说明到达上限');
      eq(st.indexOf('不完整'), -1, '到上限是预期行为，不能说"不完整"，实际: ' + st);
    });
    win.close();
  }

  // ---------------------------------------------------------------- 14 队列分批 POST
  {
    const posts = [];
    const many = [];
    for (let i = 0; i < 1200; i += 1) {
      many.push({ path: '/s/q' + i + '.jpg', name: 'q' + i + '.jpg', size: 100, ext: '.jpg', root: '/s', w: 1, h: 1 });
    }
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': { files: many, count: many.length },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': function (init) {
        const body = JSON.parse(init.body);
        posts.push(body);
        return { ok: true, count: 1200, kept: 1200, truncated: false };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    doc.getElementById('removeBtn').click();      // 触发 saveQueue（没有选中项，等价于"重存一次"）
    await tick(400);

    test('1200 条队列分批 POST（500/500/200），单批 body 远小于网关 1MB 限制', function () {
      eq(posts.length, 3, '1200 条按 500/批 应恰好 3 批，实际 ' + posts.length);
      eq(posts.map(function (p) { return p.files.length; }).join(','), '500,500,200', '每批条数');
      eq(posts[0].append, false, '第一批必须整份替换');
      eq(posts[1].append, true, '后续批次必须 append');
      eq(posts[2].append, true, '后续批次必须 append');
      const maxBody = Math.max.apply(null, posts.map(function (p) { return JSON.stringify(p).length; }));
      assert(maxBody < 500 * 1024, '单批 body 应远小于 1MB，实际 ' + maxBody + ' bytes');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 13b truncated 且落后时仍要拉完（QA S10b）
  {
    const TOTAL = 1500, PAGE = 500;
    const f = function (off, n) {
      const out = [];
      for (let i = 0; i < n; i += 1) {
        const k = off + i;
        out.push({ path: '/s/u' + k + '.jpg', name: 'u' + k + '.jpg', size: 10, ext: '.jpg', root: '/s' });
      }
      return out;
    };
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'trunc2', state: 'running' },
      // 每帧都说 done+truncated，但一帧只给 500 条：已收录的 1500 条必须全部拉到
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        return { id: 'trunc2', state: 'done', total: TOTAL, scanned: 50000,
                 files: f(off, PAGE), next_offset: off + PAGE, truncated: true };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(1600);

    test('truncated=true 且客户端落后时，已收录的 total 仍要全部拉到（1500/1500）', function () {
      eq(rows(doc).length, TOTAL, '应有 1500 行，实际 ' + rows(doc).length);
      const st = doc.getElementById('statusTxt').textContent;
      contains(st, '上限', '仍应提示到达上限');
      eq(st.indexOf('结果可能不完整'), -1, '到上限不该误报不完整');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 13c 分批失败保留自动重试机会（QA B7b）
  {
    let n = 0;
    const posts = [];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      'GET api/queue': { files: [] },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/scan': { scan_id: 'qfail', state: 'running' },
      'GET api/scan/status': function (init, url) {
        const off = parseInt((url.match(/offset=(\d+)/) || [0, 0])[1], 10);
        const out = [];
        for (let i = 0; i < 500 && off + i < 1200; i += 1) {
          const k = off + i;
          out.push({ path: '/s/q' + k + '.jpg', name: 'q' + k + '.jpg', size: 10, ext: '.jpg', root: '/s' });
        }
        return { id: 'qfail', state: 'done', total: 1200, scanned: 1200,
                 files: out, next_offset: off + out.length, truncated: false };
      },
      'POST api/queue': function (init) {
        const body = JSON.parse(init.body);
        posts.push(body);
        n += 1;
        if (n === 2) return { __raw: '<html>504 Gateway Time-out</html>' };  // 第 2 批失败一次
        return { ok: true, count: 1200, kept: 1200, truncated: false };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(150);
    await addFolderViaUI(env);
    await tick(1200);
    const failedText = doc.getElementById('statusTxt').textContent;
    const postsAtFailure = posts.length;

    test('分批保存半途失败：如实提示，且后续状态变化会自动补存（queueDirty）', function () {
      contains(failedText, '文件列表保存失败', '失败必须如实提示，不能静默吞掉');
      eq(postsAtFailure, 1 + 1, '第一批成功、第二批失败（共 2 次 POST）');
      doc.getElementById('removeBtn').click();      // 触发下一次 saveQueue
    });
    await tick(600);
    test('失败后的自动补存把队列重新发全', function () {
      assert(posts.length > postsAtFailure,
        'queueDirty 应触发补存，实际 POST 次数 ' + postsAtFailure + ' → ' + posts.length);
      eq(rows(doc).length, 1200, '文件列表不该因为保存失败而丢');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 15 v1.7.3 同名不同目录：显示所属文件夹
  {
    const two = [
      { path: '/nas/相机A/IMG_0001.JPG', name: 'IMG_0001.JPG', size: 1000, ext: '.JPG', root: '/nas', w: 100, h: 50 },
      { path: '/nas/相机B/IMG_0001.JPG', name: 'IMG_0001.JPG', size: 1000, ext: '.JPG', root: '/nas', w: 100, h: 50 }
    ];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/nas' }] },
      'GET api/queue': { files: two, count: 2 },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true }
    });
    const doc = env.doc, win = env.win;
    await tick(200);

    test('两个同名但不同目录的文件：列表各自显示所属文件夹名（不误导成"重复"）', function () {
      eq(rows(doc).length, 2, '两个文件都要在列表里');
      const dirs = rows(doc).map(function (tr) { return tr.querySelector('.fdir').textContent.trim(); });
      eq(dirs.join(','), '相机A/,相机B/', '应分别显示直接父目录名，实际 ' + dirs.join(','));
      eq(new Set(dirs).size, 2, '两个目录名必须不同');
      const fnames = rows(doc).map(function (tr) { return tr.querySelector('.p').textContent.trim(); });
      eq(fnames.join(','), 'IMG_0001.JPG,IMG_0001.JPG', '文件名相同（正是用户困惑的来源）');
      const titles = rows(doc).map(function (tr) { return tr.querySelector('.p').getAttribute('title'); });
      eq(titles.join(','), two.map(function (f) { return f.path; }).join(','), 'title 仍保留完整路径');
      eq(rows(doc)[0].children.length, 6, '表格列数不许变（仍是 6 列）');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 16 v1.7.3 重复添加：措辞改清楚
  {
    const A = '/nas/相机A', B = '/nas/相机B';
    const all = [
      { path: A + '/IMG_0001.JPG', name: 'IMG_0001.JPG', size: 1000, ext: '.JPG', root: A, w: 100, h: 50 },
      { path: B + '/IMG_0001.JPG', name: 'IMG_0001.JPG', size: 1000, ext: '.JPG', root: B, w: 100, h: 50 }
    ];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.3' },
      'GET api/fs/roots': { roots: [{ path: '/nas' }] },
      'GET api/fs/list': { dirs: [], files: [] },
      // 刷新页面后队列恢复了 A 的那个文件
      'GET api/queue': { files: [all[0]], count: 1 },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/scan': { scan_id: 'dup173', state: 'running' },
      'GET api/scan/status': function () {
        return { id: 'dup173', state: 'done', total: 2, scanned: 2, truncated: false,
                 files: all, next_offset: 2, error: null };
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    await addFolderViaUI(env);
    await tick(900);

    test('重复添加：说清「同一路径已在列表中、未重复添加、可清空重加」，不算错误', function () {
      const st = doc.getElementById('statusTxt').textContent;
      contains(st, '已在列表中', '应说明是同一路径已在列表中');
      contains(st, '未重复添加', '应说明没有丢文件');
      contains(st, '清空', '应告知如何全部重加');
      eq(st.indexOf('跳过 1 个重复'), -1, '不许再用「跳过 N 个重复」这种像丢文件的措辞');
      eq(doc.getElementById('statusTxt').classList.contains('err'), false,
        'scanDup 不是错误，状态栏不该标红');
      eq(rows(doc).length, 2, '两个不同路径的文件都必须留在列表里');
      const dirs = rows(doc).map(function (tr) { return tr.querySelector('.fdir').textContent.trim(); });
      eq(new Set(dirs).size, 2, '两个文件应显示不同目录名（一眼看出不是误杀）');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 17 v1.7.4 GIF 直通：已复制状态
  {
    const queue = [
      { path: '/s/a.jpg', name: 'a.jpg', size: 1000, ext: '.jpg', root: '/s', w: 10, h: 10 },
      { path: '/s/b.gif', name: 'b.gif', size: 2000, ext: '.gif', root: '/s', w: 20, h: 20 },
      { path: '/s/c.gif', name: 'c.gif', size: 3000, ext: '.gif', root: '/s', w: 30, h: 30 }
    ];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.4' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': { files: queue },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/compress': { task_id: 'gif123456' },
      'GET api/tasks/gif123456': {
        id: 'gif123456', state: 'done', total: 3, done: 3, ok: 1, copied: 1, skipped: 1, fail: 0,
        output: '/out', keep_structure: true, elapsed: 1,
        results: [
          { path: '/s/a.jpg', status: 'ok', out: '/out/a.jpg', in_size: 1000, out_size: 500 },
          { path: '/s/b.gif', status: 'copied', out: '/out/b.gif', in_size: 2000, out_size: 2000 },
          { path: '/s/c.gif', status: 'skipped', out: '/out/c.gif' }
        ]
      }
    });
    const doc = env.doc, win = env.win;
    await tick(200);
    doc.getElementById('outPath').value = '/out';
    doc.getElementById('outPath').dispatchEvent(new win.Event('change', { bubbles: true }));
    doc.getElementById('startBtn').click();
    await tick(1600);      // poll 间隔 1200ms

    test('GIF 直通：状态列显示「已复制」、节省率显示 —（不是 0%）', function () {
      const cells = rows(doc).map(function (tr) {
        return { save: tr.children[4].textContent.trim(), st: tr.children[5].textContent.trim() };
      });
      eq(cells[1].st, '已复制', 'GIF 应显示「已复制」，实际 ' + cells[1].st);
      eq(cells[1].save, '—', 'GIF 的节省率应显示 —（直通复制没有节省）');
      eq(cells[0].st, '成功', 'jpg 仍显示「成功」');
      eq(cells[2].st, '已跳过', '跳过仍是「已跳过」');
    });

    test('完成提示与统计栏含「已复制 N 个（GIF 不压缩）」', function () {
      contains(doc.getElementById('statusTxt').textContent, '已复制 1 个（GIF 不压缩）', '完成提示');
      contains(doc.getElementById('sumSize').textContent, '已复制 1', '统计栏');
    });

    test('「继续压缩（已完成 N）」把已复制计入已完成', function () {
      eq(doc.getElementById('startBtn').textContent, '继续压缩（已完成 3）',
        '成功1 + 已跳过1 + 已复制1 = 3');
    });

    test('日志里 GIF 标注「已复制：GIF 不压缩」', function () {
      contains(doc.getElementById('logBody').textContent, '已复制：GIF 不压缩', '日志');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 18 v1.7.4 断点续传恢复 copied 状态
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.4' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': { files: [
        { path: '/s/b.gif', name: 'b.gif', size: 2000, ext: '.gif', root: '/s', w: 20, h: 20 }
      ] },
      // 刷新页面后 results/status 返回 copied 记录
      'POST api/results/status': { done: { '/s/b.gif': { status: 'copied', out: '/out/b.gif', in_size: 2000, out_size: 2000 } } },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true }
    });
    const doc = env.doc, win = env.win;
    await tick(250);

    test('刷新页面：copied 记录恢复成「已复制」，不会显示成「成功」或要求重压', function () {
      eq(rows(doc).length, 1);
      eq(rows(doc)[0].children[5].textContent.trim(), '已复制', '状态列');
      eq(rows(doc)[0].children[4].textContent.trim(), '—', '节省率');
      eq(doc.getElementById('startBtn').textContent, '继续压缩（已完成 1）', '已完成数应含 copied');
    });
    win.close();
  }

  // ---------------------------------------------------------------- 15 分页（v1.7.5）
  {
    const N = 12000;
    const q = [];
    for (let i = 0; i < N; i++) {
      q.push({ path: '/s/p' + i + '.jpg', name: 'p' + i + '.jpg', size: 1000, ext: '.jpg', root: '/s' });
    }
    const bodies = [];
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.5' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': { files: q },
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true },
      'POST api/compress': function (init) { bodies.push(JSON.parse(init.body)); return { task_id: 'pg1' }; },
      'GET api/tasks/pg1': { id: 'pg1', state: 'done', total: N, done: N, ok: N, skipped: 0, fail: 0,
                             output: '/out', keep_structure: true, elapsed: 1, results: [] }
    });
    const doc = env.doc, win = env.win;
    await tick(400);

    test('分页：12000 条只渲染第一页 5000 行，页码显示 1/3', function () {
      eq(doc.getElementById('count').textContent, '12000 个项目');
      eq(rows(doc).length, 5000, '当前页只允许 5000 行');
      eq(doc.getElementById('pageInfo').textContent, '第 1 / 3 页');
      eq(doc.getElementById('pageRange').textContent, '1-5000 / 12000');
      eq(doc.getElementById('pagePrev').disabled, true, '第一页不能上一页');
      eq(doc.getElementById('pageNext').disabled, false);
      eq(doc.getElementById('pager').classList.contains('hidden'), false);
    });

    test('分页：翻到最后一页只剩 2000 行，首行是第 10001 条', function () {
      doc.getElementById('pageLast').click();
      eq(doc.getElementById('pageInfo').textContent, '第 3 / 3 页');
      eq(rows(doc).length, 2000, '12000 - 2*5000');
      eq(doc.getElementById('pageRange').textContent, '10001-12000 / 12000');
      eq(doc.getElementById('pageNext').disabled, true);
      eq(doc.getElementById('pagePrev').disabled, false);
      // 行上带的是**全局索引**，点击/勾选必须落到真实的那条
      const tr = rows(doc)[0];
      eq(tr.getAttribute('data-i'), '10000', '第一行的 data-i 应是 10000');
      const cb = tr.querySelector('.cb');
      cb.checked = true;
      cb.dispatchEvent(new win.Event('change', { bubbles: true }));
      assert(cb.closest('tr').classList.contains('sel'), '勾选后行要高亮');
    });

    test('分页：切换每页条数 1000 后总页数变成 12，且停留在附近位置', function () {
      doc.getElementById('pageFirst').click();
      doc.getElementById('pageSize').value = '1000';
      doc.getElementById('pageSize').dispatchEvent(new win.Event('change', { bubbles: true }));
      eq(doc.getElementById('pageInfo').textContent, '第 1 / 12 页');
      eq(rows(doc).length, 1000);
      eq(doc.getElementById('pageRange').textContent, '1-1000 / 12000');
    });

    // 点「压缩」：大列表不该把 12000 条 files 塞进请求体
    doc.getElementById('outPath').value = '/out';
    doc.getElementById('outPath').dispatchEvent(new win.Event('change', { bubbles: true }));
    doc.getElementById('startBtn').click();
    await tick(400);
    test('分页：大列表提交走「队列引用」，请求体里不带 12000 条 files', function () {
      assert(bodies.length >= 1, '应发出 /api/compress');
    });

    test('分页：use_queue=true 且 body 很小（不撞网关 1MB）', function () {
      assert(bodies.length >= 1, '应发出 /api/compress');
      const b = bodies[bodies.length - 1];
      eq(b.use_queue, true, '大列表应改用队列引用');
      assert(!b.files, '不该把 12000 条 files 塞进请求体');
      assert(JSON.stringify(b).length < 2000, '请求体应远小于 1MB，实际 ' + JSON.stringify(b).length);
    });

    win.close();
  }

  // ---------------------------------------------------------------- 16 分页边界：小列表不显示分页条
  {
    const env = makeDom({
      'GET api/status': { clt: true, version: '1.7.5' },
      'GET api/fs/roots': { roots: [{ path: '/s' }] },
      'GET api/queue': FILE_QUEUE,
      'POST api/results/status': { done: {} },
      'GET api/tasks/active': { task: null },
      'POST api/queue': { ok: true }
    });
    const doc = env.doc, win = env.win;
    await tick(250);
    test('分页：只有 3 个文件时不分页（仍 3 行），提交也走普通 files 路径', function () {
      eq(rows(doc).length, 3);
      const pager = doc.getElementById('pager');
      eq(pager.classList.contains('hidden'), true, '3 个文件不该露出分页条');
      eq(doc.getElementById('pageInfo').textContent, '第 1 / 1 页');
      eq(doc.getElementById('pageNext').disabled, true);
    });
    win.close();
  }

  console.log('\n== ' + passed + ' passed, ' + failures.length + ' failed ==');
  failures.forEach(function (f) {
    console.log('\n---- ' + f[0] + ' ----\n' + f[1]);
  });
  process.exit(failures.length ? 1 : 0);
}

main().catch(function (e) {
  console.error(e);
  process.exit(1);
});
