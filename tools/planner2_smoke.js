/**
 * 行程工作台 v2（static/planner2.html）的运行时验证（jsdom 真跑脚本，零真实网络）。
 * 风格照抄 support_smoke.js：JSDOM + beforeParse 注入 fetch stub / localStorage，
 * 无真实网络——页面所有 fetch 都按 URL 分发到 stub，未知 URL 也回 200 {}，
 * 绝不放行到真网络（CI 没有后端，2026-10-07 在 support_smoke 上实锤过一次）。
 *
 * jsdom 环境固有限制（非页面 bug）：
 *   - 外链 <script src="/static/vendor/leaflet.js"> 不执行 ⇒ window.L undefined，
 *     页面已守卫（ensureMap/drawMap 直接 return），顺带验证了这条降级路径。
 *   - 无 hover ⇒ .tl-ops 的悬停显形测不了，断言按钮在 DOM 即可（:focus-within 才是真路径）。
 *   - container-type / color-mix / oklch 等 CSS 不求值，忽略解析噪声。
 *
 * 覆盖范围（9 组，对应冒烟简报）：
 *   ① 启动：零 JS 运行时错误；城市 datalist 2 个 option（GET /cities）
 *   ② 抽取：填 textarea + #p2-go → POST /extract → 候选卡 2 张 + 「候选 2」+ 叙事「读到 2 个候选」
 *   ③ 挑卡：点第 2 张（needs_review）→ off + 计数 1；再点恢复
 *   ④ 求解：#p2-plan → POST /plan/async + 轮询 /task/:id → daytab×2 + .tl-row×2
 *           + 总览条「总门票 ¥30」+ 未排入 chip「时间窗装不下」
 *   ⑤ 行内编辑：首行 .tlop 在 DOM；点「测试博物馆」行的 ✕ 移除 → POST /plan/recheck
 *           且请求体 days 不含「测试博物馆」→ 按 stub 新 result 重渲染 1 行
 *   ⑥ 对话编辑：#p2-editinput 回车 → POST /plan/edit → 轮询新任务 → 渲染出现「大雁塔」
 *   ⑦ 401：/extract 一次 401 → auth-toast 显示、不崩、不自动重试
 *   ⑧ reduce：matchMedia stub 全命中 → 主题走暗色分支、零报错（动效归 CSS 层）
 *   ⑨ 全程 window 无未捕获异常（所有 boot 的 jsdomError/console.error 汇总 = 0）
 *
 * ⚠️ 已知页面 bug（本测试 2026-10-12 抓到，页面只读不改——修复后组⑥自动转绿，测试无需动）：
 *   根因：poll()（planner2.html :792）声明了 onDone 形参但函数体从不调用，
 *         :773（doPlan）与 :1055（doEditChat）传入的 `function(snap){ S.result = snap.result; }`
 *         是死代码 ⇒ 异步任务完成后 S.result 永不更新（保持 null，直到某次 recheck 赋值）。
 *   症状A（组⑥红）：对话编辑成功（叙事「改好了」、S.days 已换新）但画布不更新——
 *         doEditChat :1055 renderResult(S.result) 渲染的是编辑前旧快照/null。
 *   症状B（探针证实，未入断言）：新计划完成后点天 tab 无反应——bindDayTabs :924
 *         renderResult(S.result=null) 早退静默失效；做过任一次 recheck 后才恢复。
 *   修法方向（页面侧一行）：poll 在 resolve 前 `if (onDone) onDone(snap);`，
 *         或 doEditChat 改 renderResult(snap.result)；修 B 需前者。
 *
 * 用法：
 *   node tools/planner2_smoke.js
 *   node tools/planner2_smoke.js /path/to/other.html      # 测指定版本（反向验证用）
 * 需要 jsdom（tools/node_modules 已装，require 从 tools/ 自行解析到；
 * CI 里 `npm install jsdom@30.1.1 --no-save --prefix tools`）。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const REPO = path.resolve(__dirname, '..');
const HTML_PATH = process.argv[2] || path.join(REPO, 'static', 'planner2.html');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}
const wait = ms => new Promise(r => setTimeout(r, ms));
/* 轮询到条件成立为止（页面轮询间隔 900ms，写死 sleep 会又慢又脆） */
async function waitFor(fn, timeout) {
  const t0 = Date.now();
  for (;;) {
    let v = false;
    try { v = !!fn(); } catch (e) { v = false; }
    if (v) return true;
    if (Date.now() - t0 > (timeout || 3000)) return false;
    await wait(40);
  }
}

/* ---- fixtures：形状取自任务契约（= 后端真实快照形状），不是凭空编的 ---- */
const CITIES = { cities: ['西安', '成都'], default: '西安' };
const EXTRACT = {
  count: 2,
  spots: [
    { source_id: 1, name: '钟楼', lat: 34.26, lon: 108.94, stay_min: 45, score: 7.5,
      ticket: 30, ticket_known: true, open_h: 8, close_h: 18,
      desc: '市中心地标', confidence: 0.95, needs_review: false },
    { source_id: 2, name: '测试博物馆', lat: 34.25, lon: 108.95, stay_min: 90, score: 8,
      ticket: 0, ticket_known: false, open_h: 9, close_h: 17,
      desc: '...', confidence: 0.6, needs_review: true },
  ],
  needs_review_count: 1, city: '西安', detected_city: '西安', needs_city: false,
};
const PLAN_RUNNING = {
  task_id: 'aabbccddeeff', status: 'running',
  progress: ['求解中 第1天'], result: null, error: null,
};
const PLAN_DONE = {
  task_id: 'aabbccddeeff', status: 'completed',
  progress: ['求解中 第1天', '求解完成'],
  result: {
    city: '西安',
    days: [{
      day: 1,
      spots: [
        { name: '钟楼', arrive_h: 9.5, depart_h: 10.75, ticket: 30, desc: '市中心地标', ticket_known: true },
        { name: '测试博物馆', arrive_h: 11.5, depart_h: 13, ticket: 0, desc: '', ticket_known: false },
      ],
      commute_min: 30, cost: 30, active_min: 135,
    }],
    total_cost: 30, total_score: 15.5,
    unplanned: [{ name: '远郊景点', reason: '时间窗装不下' }],
    check_report: { stats: { spots_planned: 2 } },
    cost_known: true,
  },
  error: null,
};
/* recheck 契约：移除「测试博物馆」后服务端全量重算 → 1 天只剩钟楼、未排入清空 */
const RECHECK_SNAP = {
  task_id: 'aabbccddeeff', status: 'completed', progress: ['重排完成'],
  result: {
    city: '西安',
    days: [{
      day: 1,
      spots: [{ name: '钟楼', arrive_h: 9.5, depart_h: 10.75, ticket: 30, desc: '市中心地标', ticket_known: true }],
    }],
    commute_min: 10, cost: 30, active_min: 45,
    total_cost: 30, total_score: 7.5,
    unplanned: [],
    check_report: { stats: { spots_planned: 1 } },
    cost_known: true,
  },
  error: null,
};
const EDIT_TASK = { task_id: '112233445566' };
const EDIT_SNAP = {
  task_id: '112233445566', status: 'completed', progress: ['对话编辑完成'],
  result: {
    city: '西安',
    days: [{
      day: 1,
      spots: [
        { name: '钟楼', arrive_h: 9.5, depart_h: 10.5, ticket: 30, desc: '市中心地标', ticket_known: true },
        { name: '大雁塔', arrive_h: 11, depart_h: 12.5, ticket: 0, desc: '', ticket_known: false },
      ],
    }],
    commute_min: 40, cost: 30, active_min: 120,
    total_cost: 30, total_score: 13.5,
    unplanned: [],
    check_report: { stats: { spots_planned: 2 } },
    cost_known: true,
  },
  error: null,
};

/* jsdom 不实现 fetch；CSS 解析噪声（oklch/color-mix/@keyframes）与 canvas 一律忽略 */
const IGNORE = /getContext|Not implemented|Could not parse CSS/i;
const ALL_ERRS = [];   // ⑨ 全程未捕获异常汇总（跨所有 boot）

function boot(opts) {
  opts = opts || {};
  const calls = {
    cities: 0, extract: 0, planAsync: 0, recheck: 0, edit: 0,
    taskOld: 0, taskNew: 0, demoSpots: 0, demoConfig: 0, weather: 0, other: 0,
  };
  const bodies = { extract: [], planAsync: [], recheck: [], edit: [] };
  const state = { extractStatus: 200, taskPolls: 0, validNames: ['钟楼', '测试博物馆', '大雁塔'] };
  const html = fs.readFileSync(HTML_PATH, 'utf-8');
  const vc = new VirtualConsole();
  const errs = [];
  vc.on('jsdomError', e => {
    const m = e && e.message || String(e);
    if (!IGNORE.test(m)) { errs.push(m); ALL_ERRS.push(m); }
  });
  vc.on('error', (...a) => {
    const s = a.join(' ');
    if (!IGNORE.test(s)) { errs.push('console.error: ' + s); ALL_ERRS.push('console.error: ' + s); }
  });

  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    url: 'http://127.0.0.1:8000/app2',
    // 关键：stub 必须在页面脚本执行之前注入（jsdom 在构造时就运行 <script>；
    // 页面随后会包一层 X-App-Token 包装器，把这里的 fetch 当 orig 捕走）
    beforeParse(w) {
      if (opts.reduce) {
        // ⑧ prefers-reduced-motion: reduce —— 页面动效由 CSS 层关，JS 只需不报错；
        //   副作用可当正向断言：主题脚本走 mq.matches=true 分支 ⇒ data-theme=dark
        w.matchMedia = function () {
          return { matches: true, media: '', onchange: null,
                   addEventListener() {}, removeEventListener() {},
                   addListener() {}, removeListener() {}, dispatchEvent() { return false; } };
        };
      }
      w.fetch = function (u, init) {
        const url = String(u);
        let body = null;
        if (init && typeof init.body === 'string') {
          try { body = JSON.parse(init.body); } catch (e) { body = null; }
        }
        const R = (status, obj) => Promise.resolve({
          ok: status >= 200 && status < 300, status,
          json: () => Promise.resolve(JSON.parse(JSON.stringify(obj))),
        });
        if (url.indexOf('/cities') === 0) { calls.cities++; return R(200, CITIES); }
        if (url.indexOf('/extract') === 0) {
          calls.extract++; bodies.extract.push(body);
          if (state.extractStatus !== 200) return R(state.extractStatus, {});
          return R(200, EXTRACT);
        }
        if (url.indexOf('/plan/async') === 0) {
          calls.planAsync++; bodies.planAsync.push(body);
          return R(200, { task_id: 'aabbccddeeff', poll_url: '/task/aabbccddeeff' });
        }
        if (url.indexOf('/plan/recheck') === 0) {
          calls.recheck++; bodies.recheck.push(body);
          // 契约校验：days 每项 {spots:[{name}]}，name 必须是合法景点名——不合法回 400
          const days = body && Array.isArray(body.days) ? body.days : null;
          const bad = !days || days.some(d => !d || !Array.isArray(d.spots) ||
            d.spots.length === 0 || d.spots.some(s => !s || state.validNames.indexOf(s.name) < 0));
          if (bad) return R(400, { detail: '未知景点，无法重排：' + JSON.stringify(body && body.days) });
          return R(200, RECHECK_SNAP);
        }
        if (url.indexOf('/plan/edit') === 0) {
          calls.edit++; bodies.edit.push(body);
          return R(200, EDIT_TASK);
        }
        if (url.indexOf('/task/') === 0) {
          if (url.indexOf('aabbccddeeff') >= 0) {
            calls.taskOld++; state.taskPolls++;
            return R(200, state.taskPolls === 1 ? PLAN_RUNNING : PLAN_DONE);
          }
          if (url.indexOf('112233445566') >= 0) { calls.taskNew++; return R(200, EDIT_SNAP); }
        }
        if (url.indexOf('/demo/spots') === 0) { calls.demoSpots++; return R(200, { spots: [] }); }
        if (url.indexOf('/demo/config') === 0) { calls.demoConfig++; return R(200, {}); }
        if (url.indexOf('/weather') === 0) { calls.weather++; return R(200, { available: false }); }
        calls.other++;   // 任何没料到的 URL 也吃掉，绝不放行到真网络
        return R(200, {});
      };
      w.alert = () => {};
      w.confirm = () => true;
      if (!w.crypto) w.crypto = {};
      if (typeof w.crypto.randomUUID !== 'function') {
        w.crypto.randomUUID = () => '12345678-abcd-4abc-8abc-123456789abc';
      }
    },
  });
  return { w: dom.window, dom, calls, bodies, state, errs };
}

function txt(w, sel) {
  const el = w.document.querySelector(sel);
  return el ? (el.textContent || '') : '';
}
const INPUT_TEXT = '西安两天：第一天看钟楼，第二天逛博物馆，别排太赶';

/* phase 助手：抽取完成（候选卡就位） */
async function extractPhase(t) {
  t.w.document.getElementById('p2-text').value = INPUT_TEXT;
  t.w.document.getElementById('p2-go').click();
  return waitFor(() => t.w.document.querySelectorAll('#p2-cands .cand').length === 2, 2000);
}
/* phase 助手：求解完成（daytab 就位）——首轮 tick 立即、二轮 900ms 后 completed */
async function planPhase(t) {
  t.w.document.getElementById('p2-plan').click();
  return waitFor(() => t.w.document.querySelectorAll('#p2-result .daytab').length === 2, 5000);
}

async function main() {
  if (!fs.existsSync(HTML_PATH)) {
    console.log('找不到被测页面：' + HTML_PATH);
    process.exit(2);
  }

  /* ---------- ① 启动 ---------- */
  console.log('\n[1] 启动：零运行时错误 + 城市表');
  {
    const t = boot();
    await wait(80);
    ok('城市 datalist 有 2 个 option',
       t.w.document.querySelectorAll('#p2CityList option').length === 2,
       '实际 ' + t.w.document.querySelectorAll('#p2CityList option').length);
    ok('option 值 = 西安/成都',
       Array.from(t.w.document.querySelectorAll('#p2CityList option')).map(o => o.value).join(',') === '西安,成都');
    ok('GET /cities 恰好 1 次（启动加载）', t.calls.cities === 1, '实际 ' + t.calls.cities);
    ok('#p2-plan 初始禁用（0 候选不该能点）', t.w.document.getElementById('p2-plan').disabled === true);
    ok('① 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ② 抽取 ---------- */
  console.log('\n[2] 抽取：贴文本 → 候选卡');
  {
    const t = boot();
    await wait(80);
    const okPhase = await extractPhase(t);
    ok('候选卡 2 张', t.w.document.querySelectorAll('#p2-cands .cand').length === 2,
       '实际 ' + t.w.document.querySelectorAll('#p2-cands .cand').length);
    ok('#p2-keptcount = 「候选 2」', txt(t.w, '#p2-keptcount') === '候选 2', txt(t.w, '#p2-keptcount'));
    ok('叙事区出现「读到 2 个候选」', txt(t.w, '#p2-story').indexOf('读到 2 个候选') >= 0,
       txt(t.w, '#p2-story').slice(0, 60));
    ok('叙事行用 ok 语义类（成功态）', !!t.w.document.querySelector('#p2-story .storyline.ok'));
    ok('候选区/工具条解除 hidden',
       t.w.document.getElementById('p2-cands').hidden === false &&
       t.w.document.getElementById('p2-candtools').hidden === false);
    const c0 = t.w.document.querySelectorAll('#p2-cands .cand')[0];
    const c1 = t.w.document.querySelectorAll('#p2-cands .cand')[1];
    ok('卡 1 元数据：门票 ¥30 + 置信 0.95',
       !!c0 && c0.textContent.indexOf('门票 ¥30') >= 0 && c0.textContent.indexOf('置信 0.95') >= 0,
       c0 && c0.textContent.slice(0, 80));
    ok('卡 2 标「待确认」（needs_review）+「票价待查」（ticket_known=false）',
       !!c1 && !!c1.querySelector('.creview') && c1.textContent.indexOf('待确认') >= 0 &&
       c1.textContent.indexOf('票价待查') >= 0,
       c1 && c1.textContent.slice(0, 80));
    ok('城市框自动回填「西安」（needs_city=false 路径）',
       t.w.document.getElementById('p2-city').value === '西安',
       t.w.document.getElementById('p2-city').value);
    ok('#p2-plan 解禁（2 候选 + 有城市）', t.w.document.getElementById('p2-plan').disabled === false);
    const eb = t.bodies.extract[0] || {};
    ok('/extract 请求体 = {text, city:""}（S.city 在回填前是空的）',
       eb.text === INPUT_TEXT && eb.city === '', JSON.stringify(eb).slice(0, 120));
    ok('无多余外呼（2 个候选不该开预置库 / 无 weather / 无未知 URL）',
       t.calls.demoSpots === 0 && t.calls.weather === 0 && t.calls.other === 0,
       JSON.stringify(t.calls));
    ok('② 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ③ 挑卡 ---------- */
  console.log('\n[3] 挑卡：点第 2 张（needs_review）开关');
  {
    const t = boot();
    await wait(80);
    await extractPhase(t);
    const cards = () => t.w.document.querySelectorAll('#p2-cands .cand');
    cards()[1].click();
    ok('点第 2 张 → 变 off', cards()[1].classList.contains('off'));
    ok('计数变「候选 1」', txt(t.w, '#p2-keptcount') === '候选 1', txt(t.w, '#p2-keptcount'));
    ok('角标变「✕ 已移除」', cards()[1].querySelector('.ck').textContent.indexOf('已移除') >= 0,
       cards()[1].querySelector('.ck').textContent);
    ok('仍剩 1 个候选时 #p2-plan 保持可用',
       t.w.document.getElementById('p2-plan').disabled === false);
    cards()[1].click();
    ok('再点 → 恢复（无 off）', !cards()[1].classList.contains('off'));
    ok('计数恢复「候选 2」', txt(t.w, '#p2-keptcount') === '候选 2', txt(t.w, '#p2-keptcount'));
    ok('角标恢复「✓ 保留」', cards()[1].querySelector('.ck').textContent.indexOf('保留') >= 0);
    ok('③ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ④ 求解 + 轮询 + 渲染 ---------- */
  console.log('\n[4] 求解：#p2-plan → 轮询 → 行程画布');
  {
    const t = boot();
    await wait(80);
    await extractPhase(t);
    const rendered = await planPhase(t);
    ok('轮询后 #p2-result 出现 daytab', rendered,
       txt(t.w, '#p2-progress').slice(-60));
    const tabs = t.w.document.querySelectorAll('#p2-result .daytab');
    ok('daytab 2 个（第 1 天 + 全部）', tabs.length === 2, '实际 ' + tabs.length);
    ok('tab 文案 = 第 1 天/全部', Array.from(tabs).map(b => b.textContent).join(',') === '第 1 天,全部',
       Array.from(tabs).map(b => b.textContent).join(','));
    ok('活动天 tab 带 on 态', tabs[0] && tabs[0].classList.contains('on'));
    const rows = t.w.document.querySelectorAll('#p2-result .tl-row');
    ok('.tl-row 2 行', rows.length === 2, '实际 ' + rows.length);
    ok('第一行时间 09:30–10:45（hhmm 半角格式）',
       rows[0] && rows[0].querySelector('.tl-time').textContent === '09:30–10:45',
       rows[0] && rows[0].querySelector('.tl-time').textContent);
    ok('第二行时间 11:30–13:00',
       rows[1] && rows[1].querySelector('.tl-time').textContent === '11:30–13:00',
       rows[1] && rows[1].querySelector('.tl-time').textContent);
    ok('行名 = 1. 钟楼 / 2. 测试博物馆',
       txt(t.w, '#p2-result .tl-row .tl-name').indexOf('钟楼') >= 0 &&
       rows[1].querySelector('.tl-name').textContent.indexOf('测试博物馆') >= 0,
       rows[1] && rows[1].querySelector('.tl-name').textContent);
    ok('博物馆票档「票价待查」+ unknown 语义类',
       rows[1].querySelector('.tl-ticket').textContent === '票价待查' &&
       rows[1].querySelector('.tl-ticket').classList.contains('unknown'));
    ok('日脚注含「通勤 30 分钟」', txt(t.w, '#p2-result .tl-dayfoot').indexOf('通勤 30 分钟') >= 0,
       txt(t.w, '#p2-result .tl-dayfoot'));
    ok('总览条含「总门票 ¥30」', txt(t.w, '#p2-result .totals').indexOf('总门票 ¥30') >= 0,
       txt(t.w, '#p2-result .totals'));
    ok('总览条含「行程分 15.50」（toFixed(2)）',
       txt(t.w, '#p2-result .totals').indexOf('行程分 15.50') >= 0);
    ok('总览条含「未能排入 1」warn chip',
       txt(t.w, '#p2-result .totals').indexOf('未能排入 1') >= 0);
    ok('总览条含「校验通过 · 已排 2」（check_report.stats）',
       txt(t.w, '#p2-result .totals').indexOf('校验通过 · 已排 2') >= 0);
    const up = t.w.document.querySelectorAll('#p2-result .unplanned span');
    ok('未排入 chip 含「时间窗装不下」', up.length === 1 && up[0].textContent === '远郊景点：时间窗装不下',
       up.length && up[0].textContent);
    ok('对话编辑条解除 hidden', t.w.document.getElementById('p2-editchat').hidden === false);
    const pb = t.bodies.planAsync[0] || {};
    ok('/plan/async 请求体：city=西安 days=2',
       pb.city === '西安' && pb.days === 2, JSON.stringify(pb).slice(0, 120));
    ok('/plan/async 请求体：spots 带原始 lat/stay/score/ticket_known',
       Array.isArray(pb.spots) && pb.spots.length === 2 &&
       pb.spots[0].name === '钟楼' && pb.spots[0].lat === 34.26 &&
       pb.spots[0].stay_min === 45 && pb.spots[0].score === 7.5 &&
       pb.spots[1].name === '测试博物馆' && pb.spots[1].ticket_known === false,
       JSON.stringify(pb.spots || []).slice(0, 140));
    ok('/plan/async 请求体：时间窗 9–18 + 偏好/稳妥度缺省',
       pb.daily_start_h === 9 && pb.daily_end_h === 18 &&
       pb.preference === 'balanced' && pb.robustness === 0);
    ok('轮询恰 2 次（running → completed）', t.calls.taskOld === 2, '实际 ' + t.calls.taskOld);
    ok('进度叙事含「求解中 第1天」与「行程已生成」',
       txt(t.w, '#p2-progress').indexOf('求解中 第1天') >= 0 &&
       txt(t.w, '#p2-progress').indexOf('行程已生成') >= 0,
       txt(t.w, '#p2-progress').slice(-60));
    ok('无多余外呼（无日期不发 /weather；无 Leaflet 不拉 /demo/config；无未知 URL）',
       t.calls.weather === 0 && t.calls.demoConfig === 0 && t.calls.other === 0,
       JSON.stringify(t.calls));
    ok('④ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ⑤ 行内编辑：移除 → recheck ---------- */
  console.log('\n[5] 行内编辑：✕ 移除 → /plan/recheck → 重渲染');
  {
    const t = boot();
    await wait(80);
    await extractPhase(t);
    await planPhase(t);
    const rows = t.w.document.querySelectorAll('#p2-result .tl-row');
    const ops1 = rows[0] ? rows[0].querySelectorAll('.tlop') : [];
    ok('首行 .tlop 在 DOM（3 枚：移除/±15；单天无移天钮）',
       ops1.length === 3 && ops1[0].dataset.act === 'remove' &&
       ops1[0].textContent.indexOf('移除') >= 0,
       'ops=' + ops1.length);
    const rm = rows[1] ? rows[1].querySelector('.tlop[data-act="remove"]') : null;
    ok('第二行（测试博物馆）有「✕ 移除」', !!rm);
    if (rm) rm.click();
    await waitFor(() => t.w.document.querySelectorAll('#p2-result .tl-row').length === 1, 3000);
    ok('发出 /plan/recheck 恰 1 次', t.calls.recheck === 1, '实际 ' + t.calls.recheck);
    const rb = t.bodies.recheck[0] || {};
    ok('recheck 请求体 days = [{spots:[{name:"钟楼"}]}]（不含「测试博物馆」）',
       rb.task_id === 'aabbccddeeff' &&
       JSON.stringify(rb.days) === '[{"spots":[{"name":"钟楼"}]}]',
       JSON.stringify(rb).slice(0, 160));
    ok('按 stub 新 result 重渲染为 1 行',
       t.w.document.querySelectorAll('#p2-result .tl-row').length === 1);
    ok('旧内容消失（不再出现「测试博物馆」）',
       txt(t.w, '#p2-result').indexOf('测试博物馆') < 0);
    ok('新总览条「校验通过 · 已排 1」+ 未排入清空',
       txt(t.w, '#p2-result .totals').indexOf('校验通过 · 已排 1') >= 0 &&
       !t.w.document.querySelector('#p2-result .unplanned'));
    ok('⑤ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ⑥ 对话编辑 ---------- */
  console.log('\n[6] 对话编辑：#p2-editinput 回车 → /plan/edit');
  {
    const t = boot();
    await wait(80);
    await extractPhase(t);
    await planPhase(t);
    const inp = t.w.document.getElementById('p2-editinput');
    inp.value = '加个大雁塔';
    inp.dispatchEvent(new t.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    const done = await waitFor(() => txt(t.w, '#p2-result').indexOf('大雁塔') >= 0, 3000);
    ok('回车后 POST /plan/edit 被调（恰 1 次）', t.calls.edit === 1, '实际 ' + t.calls.edit);
    const db = t.bodies.edit[0] || {};
    ok('/plan/edit 请求体 = {task_id, instruction, memory:[]}',
       db.task_id === 'aabbccddeeff' && db.instruction === '加个大雁塔' &&
       Array.isArray(db.memory) && db.memory.length === 0,
       JSON.stringify(db).slice(0, 160));
    ok('渲染出现「大雁塔」', done, txt(t.w, '#p2-result').slice(0, 120));
    // ⚠️ 上断言当前红 = 页面真 bug（见文件头「已知页面 bug」症状A）：
    //    poll 不调 onDone ⇒ S.result 停留旧值 ⇒ renderResult 渲染编辑前快照。页面修复后转绿，测试不改。
    ok('画布 2 行（编辑后计划 = 钟楼 + 大雁塔）',
       t.w.document.querySelectorAll('#p2-result .tl-row').length === 2);
    ok('叙事出现「改好了」', txt(t.w, '#p2-progress').indexOf('改好了') >= 0,
       txt(t.w, '#p2-progress').slice(-60));
    ok('⑥ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ⑦ 401 ---------- */
  console.log('\n[7] 401：/extract 一次 401 → auth-toast');
  {
    const t = boot();
    t.state.extractStatus = 401;
    await wait(80);
    t.w.document.getElementById('p2-text').value = INPUT_TEXT;
    t.w.document.getElementById('p2-go').click();
    const shown = await waitFor(() => t.w.document.getElementById('authToast').hidden === false, 1500);
    ok('auth-toast 显示', shown);
    ok('toast 文案含「访问令牌无效」',
       txt(t.w, '#authToast').indexOf('访问令牌无效') >= 0, txt(t.w, '#authToast'));
    await wait(150);
    ok('不自动重试（/extract 恰 1 次）', t.calls.extract === 1, '实际 ' + t.calls.extract);
    ok('不落入通用失败叙事（401 短路，不出现「抽取失败」）',
       txt(t.w, '#p2-story').indexOf('抽取失败') < 0, txt(t.w, '#p2-story').slice(0, 60));
    ok('#p2-go 恢复可用（finally 兜底）',
       t.w.document.getElementById('p2-go').disabled === false);
    ok('⑦ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ⑧ reduce ---------- */
  console.log('\n[8] prefers-reduced-motion: reduce');
  {
    const t = boot({ reduce: true });
    await wait(80);
    ok('主题脚本走 mq.matches=true 分支（data-theme=dark，证明 stub 生效）',
       t.w.document.documentElement.getAttribute('data-theme') === 'dark',
       t.w.document.documentElement.getAttribute('data-theme'));
    ok('⑧ 无 JS 运行时错误', t.errs.length === 0, t.errs.join(' | '));
  }

  /* ---------- ⑨ 汇总：全程零未捕获 ---------- */
  console.log('\n[9] 全程零未捕获异常');
  ok('所有 boot 的 jsdomError/console.error 汇总 = 0（页面无一处崩）',
     ALL_ERRS.length === 0, ALL_ERRS.slice(0, 3).join(' | '));

  console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error('脚本自身出错:', e && e.stack || e); process.exit(2); });
