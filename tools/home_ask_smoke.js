/**
 * 首页「检索问答演示框」的运行时验证（jsdom 真跑脚本，不依赖真浏览器）。
 *
 * 为什么必须有这一份：
 * 静态检查只能证明语法对，证明不了「点了以后真的渲染出东西」。本项目出过一次事故——
 * `const esc = ...` 遮蔽了全局转义函数 `esc()`，静态检查（语法 / 命名 / 对比度）全绿，
 * 但详情卡渲染到一半抛错、内容大面积缺失，异常还被 `.catch()` 吞掉。
 * 所以这里除了正向断言，还给每条闸门配了**反向用例**：反例上也成立才算数。
 *
 * 覆盖范围（R1 检索 + R4 grounded 生成层的对外表现）：
 *   初始渲染 / 默认不烧 LLM / 勾选后才生成 / grounded 徽章三态 /
 *   LLM 降级不崩 / 零命中 / 请求失败 / 冷却防刷 / XSS 转义
 *
 * 用法：
 *   NODE_PATH=tools/node_modules node tools/home_ask_smoke.js
 *   NODE_PATH=tools/node_modules node tools/home_ask_smoke.js /path/to/old.html   # 验证测试有效性
 * 需要 jsdom（CI 里 `npm install jsdom@30.1.1 --no-save --prefix tools`）。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const REPO = path.resolve(__dirname, '..');
const HTML_PATH = process.argv[2] || path.join(REPO, 'static', 'home.html');

/* 真实响应快照：2026-10-04 取自 GET /ask?q=乳扇是什么&k=5&with_answer=1。
   断言全部由这份 fixture 驱动（不硬编码「乳扇」），语料变动时只需更新快照，
   脚本会报出实际值而不是含糊地绿。 */
const FIXTURE = {
  q: '乳扇是什么', city: null, k: 5,
  results: [{
    type: '美食', city: '大理', name: '乳扇',
    text: '乳扇 牛奶做的扇形干酪，炭火烤软蘸玫瑰糖 大理',
    source: '美食种子库', score: 9.551, verified: true, lat: null, lon: null,
  }],
  verified_count: 1,
  index: { docs: 598, cities: 38 },
  answer: {
    text: '乳扇是牛奶做的扇形干酪，常在大理炭火烤软后蘸玫瑰糖食用。',
    model: 'glm-4-flash-250414', grounded: true, outside: [],
  },
};

/* /cities 的 stub 响应。为什么必须单独给：页面初始化时会先拉城市列表建下拉，
   若 stub 一律回 /ask 的 fixture，d.cities 为 undefined → CITIES 为空数组 →
   城市限定功能整条链路静默失效，而其余 20 多条断言照样全绿。
   这是「显示绿但没覆盖」的坑，必须让这条路径真的被走到。 */
const CITY_LIST = ['西安', '青岛', '大理', '拉萨', '杭州', '张家界'];

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}

/* jsdom 不实现 fetch，也实现不了 canvas WebGL（hero 夜空极光要用）。
   这两条是环境固有限制而非页面 bug —— 与 tools/frontend_smoke.js 同样处理，
   hero 动效的真机断言在 tools/hero_motion_check.js。 */
const IGNORE = /getContext|Not implemented/i;

function boot(handler, opts) {
  const citiesFail = !!(opts && opts.citiesFail);
  const html = fs.readFileSync(HTML_PATH, 'utf-8');
  const vc = new VirtualConsole();
  const errs = [];
  vc.on('jsdomError', e => { if (!IGNORE.test(e.message || '')) errs.push(e.message); });
  const state = { url: null };
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    url: 'http://127.0.0.1:8000/',
    // 关键：stub 必须在页面脚本执行之前注入（jsdom 在构造时就运行 <script>）
    beforeParse(w) {
      w.fetch = function (u) {
        const url = String(u);
        if (url.indexOf('/cities') >= 0) {
          if (citiesFail) {
            return Promise.resolve({
              ok: false, status: 503, json: () => Promise.resolve({}),
            });
          }
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ cities: CITY_LIST }),
          });
        }
        state.url = url;          // 只记录 /ask，避免被初始化请求覆盖
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(handler(url)),
        });
      };
    },
  });
  return { dom, w: dom.window, errs, state };
}

// 只有带 with_answer=1 的请求才该拿到 answer —— 否则测不出「默认关」这件事
const withAnswer = d => u =>
  (/with_answer=1/.test(u || '') ? d : Object.assign({}, d, { answer: undefined }));

const chips = w => [...w.document.querySelectorAll('.askchip')];
const outText = w => w.document.getElementById('askOut').textContent;
const wait = ms => new Promise(r => setTimeout(r, ms));
const clone = o => JSON.parse(JSON.stringify(o));

async function main() {
  console.log('\n=== 首页检索问答演示框 · 运行时验证 ===\n');
  const NAME = FIXTURE.results[0].name;

  // 1. 初始渲染
  {
    const { w, errs } = boot(withAnswer(FIXTURE));
    ok('页面脚本无运行时错误', errs.length === 0, errs.join(' | '));
    ok('示例 chip 已渲染', chips(w).length > 0, '实际 ' + chips(w).length);
    ok('演示框 section 存在', !!w.document.getElementById('ask'));
    ok('既有元素未被破坏（#apiTable）', !!w.document.getElementById('apiTable'));
  }

  // 1b. 演示框整体缺失时直接判负退出：
  // 否则后面会崩在 TypeError（chips[0] undefined）而不是干净地红 ——
  // 用这份脚本去测「改动前的版本」时就是这条路径（已验证：会正确地失败）。
  {
    const { w } = boot(withAnswer(FIXTURE));
    if (!w.document.getElementById('ask') || !chips(w).length) {
      console.log('  ✗ 演示框或其示例缺失，后续用例无法运行');
      console.log('\n' + pass + '/' + (pass + 1) + ' 项通过\n');
      process.exit(1);
    }
  }

  // 2. 点示例 → 纯检索（不带生成）
  {
    const { w, state } = boot(withAnswer(FIXTURE));
    chips(w)[0].click();
    await wait(60);
    ok('点击 chip 触发 /ask 请求', /\/ask\?q=/.test(state.url || ''), state.url);
    ok('默认不叠加生成（URL 无 with_answer）', !/with_answer/.test(state.url || ''), state.url);
    ok('渲染出检索结果条目',
       w.document.querySelectorAll('.askitem').length === FIXTURE.results.length,
       '实际 ' + w.document.querySelectorAll('.askitem').length);
    ok('结果含命中名称', outText(w).includes(NAME));
    ok('默认不出现生成答案区', !w.document.querySelector('.askans'));
  }

  // 3. 勾选生成 → 答案 + grounded 徽章
  {
    const { w, state } = boot(withAnswer(FIXTURE));
    w.document.getElementById('askGen').checked = true;
    chips(w)[0].click();
    await wait(60);
    ok('勾选后 URL 带 with_answer=1', /with_answer=1/.test(state.url || ''), state.url);
    ok('渲染出生成答案区 .askans', !!w.document.querySelector('.askans'));
    ok('答案正文出现', outText(w).includes(FIXTURE.answer.text.slice(0, 12)));
    const gate = w.document.querySelector('.askgate');
    ok('出现 grounded 徽章', !!gate);
    ok('grounded=true → 标记为已通过核查',
       !!gate && gate.classList.contains('ok') && gate.textContent.includes('已通过'),
       gate ? gate.className + ' / ' + gate.textContent : 'none');
  }

  // 4. 【反向】grounded=false 必须标红 —— 闸门不能恒绿
  {
    const bad = clone(FIXTURE);
    bad.answer = { text: '乳扇和过桥米线都是大理名吃。', model: 'm', grounded: false, outside: ['过桥米线'] };
    const { w } = boot(withAnswer(bad));
    w.document.getElementById('askGen').checked = true;
    chips(w)[0].click();
    await wait(60);
    const gate = w.document.querySelector('.askgate');
    ok('【反向】grounded=false → 徽章为 bad', !!gate && gate.classList.contains('bad'),
       gate ? gate.className : 'none');
    ok('【反向】徽章列出未落地实体', !!gate && gate.textContent.includes('过桥米线'),
       gate ? gate.textContent : 'none');
  }

  // 5. 【反向】LLM 降级（text=null）不能崩、原因要可见
  {
    const deg = clone(FIXTURE);
    deg.answer = { text: null, grounded: null, outside: [], note: '生成不可用：通道挂了' };
    const { w, errs } = boot(withAnswer(deg));
    w.document.getElementById('askGen').checked = true;
    chips(w)[0].click();
    await wait(60);
    ok('【反向】降级时仍渲染检索结果', w.document.querySelectorAll('.askitem').length > 0);
    ok('【反向】降级原因对用户可见', outText(w).includes('通道挂了'));
    ok('【反向】降级不抛运行时错误', errs.length === 0, errs.join(' | '));
  }

  // 6. 零命中
  {
    const empty = { q: '不存在的问题XYZ', results: [], verified_count: 0, index: { docs: 598 } };
    const { w } = boot(withAnswer(empty));
    chips(w)[0].click();
    await wait(60);
    ok('零命中时给出说明而非空白', outText(w).includes('没有命中'));
  }

  // 7. 请求失败
  {
    const { w } = boot(withAnswer(FIXTURE));
    w.fetch = () => Promise.reject(new Error('HTTP 500'));
    chips(w)[0].click();
    await wait(60);
    ok('请求失败时给出可读错误', outText(w).includes('取不到结果'));
    ok('失败文案含具体原因', outText(w).includes('HTTP 500'));
  }

  // 8. 冷却（公开接口，额度是全场共享的）
  {
    const { w } = boot(withAnswer(FIXTURE));
    chips(w)[0].click();
    await wait(60);
    chips(w)[0].click();
    ok('连点触发冷却提示（防刷配额）', outText(w).includes('慢一点'));
  }

  // 9. 转义：语料里的标签不能被解析成元素
  {
    const evil = clone(FIXTURE);
    evil.results = [{
      type: '美食', city: 'X', name: '<img src=x onerror=alert(1)>',
      text: '<script>bad()</script>', score: 1, verified: false,
    }];
    evil.verified_count = 0;
    evil.answer = { text: '<b>不该被解析</b>', model: 'm', grounded: true, outside: [] };
    const { w } = boot(withAnswer(evil));
    w.document.getElementById('askGen').checked = true;
    chips(w)[0].click();
    await wait(60);
    ok('语料里的标签被转义（无注入元素）',
       w.document.querySelectorAll('.askitem img, .askitem script, .askans b').length === 0);
    ok('转义后原文仍可读', outText(w).includes('<script>'));
  }

  // 10. 城市范围限定 —— 跨城市污染的止血
  //     「西安有什么好吃的」在全库检索下会靠 "吃的" 这个 bigram 把青岛·流亭猪蹄
  //     捞到第 1（实测打分 6.937 vs 西安博物院 5.955）。限定城市后污染消失。
  const askForm = w => w.document.getElementById('askForm');
  const askCity = w => w.document.getElementById('askCity');
  const submitWith = (w, q) => {
    w.document.getElementById('askInput').value = q;
    askForm(w).dispatchEvent(new w.Event('submit', { cancelable: true }));
  };
  const cityParam = url => {
    const m = /city=([^&]*)/.exec(url || '');
    return m ? decodeURIComponent(m[1]) : null;
  };

  {
    const { w, state } = boot(withAnswer(FIXTURE));
    await wait(60);   // 等 /cities 的 stub 响应落地，否则 CITIES 还是空数组
    if (!askCity(w)) {
      // 没有城市下拉的旧版本：干净判负并跳过这组，不要崩在 TypeError 上
      ok('城市下拉已填充（含「不限城市」）', false, '#askCity 不存在');
      console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
      process.exit(1);
    }
    ok('城市下拉已填充（含「不限城市」）',
       askCity(w).options.length === CITY_LIST.length + 1,
       '实际 ' + askCity(w).options.length);

    submitWith(w, '西安有什么好吃的');
    await wait(60);
    ok('问题含城市名 → 自动限定该城市',
       cityParam(state.url) === '西安', state.url);
    ok('限定后 URL 仍带查询词', /q=/.test(state.url || ''), state.url);
  }

  {
    const { w, state } = boot(withAnswer(FIXTURE));
    await wait(60);
    askCity(w).value = '青岛';
    askCity(w).dispatchEvent(new w.Event('change'));   // 手动选择 → 以用户为准
    submitWith(w, '西安有什么好吃的');
    await wait(60);
    ok('手动选过城市 → 不被 query 里的城市名覆盖',
       cityParam(state.url) === '青岛', state.url);
  }

  {
    const { w, state } = boot(withAnswer(FIXTURE));
    await wait(60);
    submitWith(w, '乳扇是什么');        // 不含任何城市名
    await wait(60);
    ok('问题不含城市名 → 不限定（URL 无 city）',
       cityParam(state.url) === null, state.url);
    ok('不限定时仍能正常检索', /\/ask\?q=/.test(state.url || ''), state.url);
  }

  // 10d. /cities 取不到时的降级：不能挡住主流程
  {
    const { w, state, errs } = boot(withAnswer(FIXTURE), { citiesFail: true });
    await wait(60);
    submitWith(w, '西安有什么好吃的');
    await wait(60);
    ok('城市列表取不到时不报错', errs.length === 0, errs.join(' | '));
    ok('城市列表取不到时检索照常可用', /\/ask\?q=/.test(state.url || ''), state.url);
  }

  console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error('脚本自身出错:', e); process.exit(1); });
