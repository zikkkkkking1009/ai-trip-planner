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

function makeFetchStub() {
  return (url) => {
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
  vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.message || e)));
  vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

  // 关键：stub 必须在页面脚本执行之前注入（jsdom 在构造时就运行 <script>），
  // 因此用 beforeParse 钩子；事后赋 window.fetch 已太晚，页面首屏 fetch 会直接报错。
  class FakeWS {
    constructor() {
      setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({
        status: 'completed',
        progress: [
          { stage: '构造', msg: '冒烟：已完成' },
          // 2026-09-23 加：后端会把 LLM 原始输出写进「调试」阶段推给前端，
          // 所以进度日志的 stage/msg 必须当不可信输入处理。这里塞一个注入载荷验证转义。
          { stage: '调试', msg: '模型原始输出: <img id="xss-probe" src=x onerror="window.__pwned=1">' },
        ],
        result: fakePlan(),
      }) }), 20);
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

  // 4) 转义：带引号与尖括号的景点名不得被当作标签解析
  const nameEl = d.querySelector('#spotModal [data-act]') || d.querySelector('#spotModal b');
  const injected = d.querySelectorAll('#spotModal 标签').length;
  check('转义：外部名字中的尖括号未被解析为标签', injected === 0);

  // 5) 进度日志转义：后端推来的 stage/msg（含 LLM 原始输出）不得被解析成 DOM
  //    2026-09-23 修：log() 曾把 stage/msg 直接插进 innerHTML。
  const logEl = d.getElementById('log');
  check('转义：进度日志里的注入载荷未变成元素',
        d.querySelectorAll('#log #xss-probe').length === 0
        && d.querySelectorAll('#log img').length === 0);
  check('转义：进度日志把载荷当纯文本保留',
        (logEl?.textContent || '').includes('xss-probe'));

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
