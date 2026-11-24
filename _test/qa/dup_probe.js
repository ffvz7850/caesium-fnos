#!/usr/bin/env node
/* 探针：不同文件夹里的同名文件，会不会被前端判成「重复」并跳过。
   用法：node dup_probe.js  （从 _test/qa 目录跑） */
'use strict';
const { execFileSync } = require('child_process');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = path.join(ROOT, 'caesium', 'app');
const PAGE = execFileSync('python', ['-c',
  'import sys; sys.path.insert(0,' + JSON.stringify(APP) + '); import server; sys.stdout.write(server.PAGE)'],
  { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });

const A = '/nas/相机A', B = '/nas/相机B';
const NAMES = ['IMG_0001.JPG', 'IMG_0002.JPG', 'DSC_9999.JPG'];
const ALL = [];
for (const dir of [A, B]) for (const n of NAMES) {
  ALL.push({ path: dir + '/' + n, name: n, size: 1000, ext: '.JPG', root: dir, w: 100, h: 50 });
}

function tick(ms) { return new Promise(r => setTimeout(r, ms || 60)); }

function mk(all, label, preload) {
  return new Promise(async (resolve) => {
    const calls = [];
    const vc = new VirtualConsole(); vc.on('jsdomError', function () {});
    const dom = new JSDOM(PAGE, {
      runScripts: 'dangerously', url: 'http://127.0.0.1:8390/app/caesium/', virtualConsole: vc,
      beforeParse: function (w) {
        w.alert = function () {}; w.confirm = function () { return true; };
        // 走真实路径：授权回调把多个目录放进 sessionStorage，页面加载时 IIFE 自动 addPaths(pend,true)
        try { w.sessionStorage.setItem('caesium.pendingPaths', JSON.stringify([A, B])); } catch (e) {}
        w.fetch = function (input, init) {
          const url = String(input).replace(/^\.?\//, '');
          const method = ((init && init.method) || 'GET').toUpperCase();
          const key = method + ' ' + url.split('?')[0];
          calls.push(key);
          let payload = { ok: true };
          if (key === 'POST api/scan') payload = { scan_id: 'probe1', state: 'running', errors: [] };
          else if (key === 'GET api/scan/status') {
            const m = /offset=(\d+)/.exec(url);
            const off = m ? parseInt(m[1], 10) : 0;
            const lim = /limit=(\d+)/.exec(url);
            const n = lim ? parseInt(lim[1], 10) : 500;
            const chunk = all.slice(off, off + n);
            payload = { id: 'probe1', state: 'done', scanned: all.length, total: all.length,
              files: chunk, offset: off, next_offset: off + chunk.length, truncated: false, error: null };
          } else if (key === 'GET api/status') payload = { clt: true, version: '1.7.2' };
          else if (key === 'GET api/fs/roots') payload = { roots: [{ path: '/nas' }] };
          else if (key === 'GET api/queue') payload = { files: preload || [] };
          else if (key === 'GET api/tasks/active') payload = { task: null };
          else if (key === 'POST api/results/status') payload = { done: {} };
          return Promise.resolve({ ok: true, status: 200,
            json: () => Promise.resolve(payload), text: () => Promise.resolve(JSON.stringify(payload)) });
        };
      }
    });
    const w = dom.window, doc = w.document;
    await tick(900);
    const rowCount = doc.querySelectorAll('#tbody tr').length;
    const status = (doc.getElementById('statusTxt') || {}).textContent || '';
    const pathsInDom = [];
    // 从表格标题/预览注记里能看出文件名，行数是关键
    console.log('[' + label + '] 服务端返回 ' + all.length + ' 条，表格行数=' + rowCount +
      '，状态栏=' + JSON.stringify(status.trim()));
    console.log('  含「重复」提示 = ' + /重复/.test(status));
    dom.window.close();
    resolve({ rowCount, status });
  });
}

(async () => {
  const r1 = await mk(ALL, '两个不同文件夹、各有 3 个同名文件');

  // 场景 2：大批量跨页（A/B 各 600 个同名文件 → 1200 条，3 页）
  const BIG = [];
  for (const dir of [A, B]) for (let i = 0; i < 600; i += 1) {
    const n = 'IMG_' + String(i).padStart(4, '0') + '.JPG';
    BIG.push({ path: dir + '/' + n, name: n, size: 1000, ext: '.JPG', root: dir, w: 100, h: 50 });
  }
  const r2 = await mk(BIG, '1200 条跨 3 页、两目录文件名完全一一对应');

  // 场景 3：页面刷新后队列恢复了 A 里的 3 个文件，再添加 [A, B]
  const pre = ALL.slice(0, 3).map(f => ({ path: f.path, name: f.name, size: f.size,
    ext: f.ext, root: f.root, w: f.w, h: f.h }));
  const r3 = await mk(ALL, '列表已有 A 的 3 个文件，再添加 [A,B]', pre);

  const ok = r1.rowCount === 6 && !/重复/.test(r1.status) &&
             r2.rowCount === 1200 && !/重复/.test(r2.status);
  console.log(ok ? '结论：前端按完整 path 去重，同名不同目录不误判 → PASS'
                 : '结论：前端误判重复 → FAIL，需继续定位');
})();
