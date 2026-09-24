/**
 * 酒店弹层 + 长图 的运行时冒烟（jsdom 真跑，不依赖浏览器）。
 *
 * 为什么单独写：这几轮改动集中在酒店弹层（分页/筛选/画廊/更换）与长图（预览），
 * 而静态检查只看语法与命名 —— 分页追加、筛选过滤、事件绑定、`let` 作用域这类问题
 * 只有真的执行一遍并断言 DOM 才抓得到。
 *
 * 用法：node tools/hotel_smoke.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const REPO = path.resolve(__dirname, '..');
const HTML_PATH = path.join(REPO, 'static', 'index.html');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 第 1 页给满 25 条（模拟"还有下一页"），第 2 页只给 3 条（模拟到底）
const hotelsForPage = (page) => {
  const n = page === 1 ? 25 : 3;
  return {
    city: '西安', page,
    results: Array.from({ length: n }, (_, k) => ({
      name: `测试酒店${page}-${k}`, lat: 34.26 + k * 0.001, lon: 108.94,
      intro: '宾馆酒店', image: 'http://img/h.jpg',
      rating: k % 2 ? '4.6' : '3.8', price: '',
      keytag: '高档型', grade: k % 2 ? '高档型' : '经济型',
      adname: '碑林区', address: '测试路 1 号', tel: '029-0000000',
      photos: ['http://img/h1.jpg', 'http://img/h2.jpg', 'http://img/h3.jpg'],
    })),
  };
};

function makeFetchStub(counter) {
  const res = obj => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(obj) });
  return (url, opt) => {
    const u = String(url);
    if (u.includes('/hotel/recommend') || u.includes('/hotel/search')) {
      counter.hotel++;
      let page = 1;
      try { page = JSON.parse((opt && opt.body) || '{}').page || 1; } catch (e) {}
      return res(hotelsForPage(page));
    }
    if (u.includes('/hotel/set')) { counter.set++; return res({ task_id: 'x', poll_url: '/task/x' }); }
    if (u.includes('/demo/config')) return res({ tile_url: '', subdomains: '', attribution: 't', gcj: true });
    if (u.includes('/demo/spots')) return res({ city: '西安', spots: [] });
    if (u.includes('/cities')) return res({ cities: ['西安'], centers: { '西安': [34.26, 108.94] }, default: '西安' });
    if (u.includes('/favorites')) return res({ favorites: [] });
    if (u.includes('/plans')) return res({ plans: [] });
    if (u.includes('/plan/async')) return res({ task_id: 'smoke', ws_url: '/ws/smoke' });
    return res({});
  };
}

function fakePlan() {
  return {
    city: '西安', total_cost: 30, total_score: 8, hotel: null,
    check_report: { passed: true, violations: [],
      stats: { spots_planned: 1, commute_api: { api_calls: 0 }, cache_hit_rate: 1 } },
    unplanned: [], reply: '', changes: [],
    days: [{ day: 1, commute_min: 12, cost: 30, active_min: 60, spots: [] }],
  };
}

// jsdom 不会加载 <script src=...leaflet.js>，给个最小桩，免得调用处抛错
function leafletStub() {
  const chain = () => { const o = {}; ['setView','invalidateSize','fitBounds','remove','addTo',
    'removeLayer','bindPopup','addLayer'].forEach(m => { o[m] = () => o; }); return o; };
  return { map: chain, tileLayer: chain, polyline: chain, circleMarker: chain,
           marker: chain, layerGroup: chain, divIcon: () => ({}) };
}

(async () => {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const errors = [];
  const vc = new VirtualConsole();
  const IGNORE = /Could not parse CSS stylesheet/;
  const pushErr = s => { if (!IGNORE.test(s)) errors.push(s); };
  vc.on('jsdomError', e => pushErr('jsdomError: ' + (e.message || e)));
  vc.on('error', (...a) => pushErr('console.error: ' + a.join(' ')));

  const counter = { hotel: 0, set: 0 };
  class FakeWS {
    constructor() {
      setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({
        status: 'completed', progress: [], result: fakePlan() }) }), 20);
    }
    close() {} send() {}
  }

  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:8000/', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = makeFetchStub(counter);
      w.WebSocket = FakeWS;
      w.L = leafletStub();
      w.alert = () => { w.__alerted = (w.__alerted || 0) + 1; };
      w.confirm = () => true;
    },
  });
  const { window } = dom;
  await sleep(60);

  const d = window.document;
  // 首屏：右栏不该是空白（曾因初始化没调 renderItin 而全白，已修，这里防回归）
  const initView = (d.getElementById('view') || {}).textContent || '';
  const results = [];
  const check = (name, ok, detail = '') => {
    results.push({ name, ok });
    console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  };
  const click = el => el && el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  // ---- 酒店弹层 ----
  check('首屏右栏非空（空状态引导卡）', initView.trim().length > 10, initView.trim().slice(0, 18));
  check('openHotelPicker 是全局函数', typeof window.openHotelPicker === 'function');
  window.openHotelPicker();
  await sleep(90);
  check('弹层已打开', !!d.getElementById('hotelModal'));
  const cardCount = () => d.querySelectorAll('#hotelResults .hpk-card').length;
  check('打开即推荐（无需输入关键词）', cardCount() > 0, `cards=${cardCount()}`);
  check('后端被调用 1 次', counter.hotel === 1, `calls=${counter.hotel}`);
  check('计数文案含"已加载"', /已加载/.test((d.getElementById('hotelCount') || {}).textContent || ''));

  // 筛选条
  const fchips = [...d.querySelectorAll('#hotelFilters .hpk-fchip')].map(e => e.textContent.trim());
  check('筛选 chip 用归一化档位', fchips.some(t => t.includes('经济型')) && fchips.some(t => t.includes('高档型')), fchips.join(' | '));
  check('筛选 chip 无原始 keytag 噪音', !fchips.some(t => t.includes('中餐') || t.includes('住宿服务')), '');

  // 点「经济型」→ 只剩经济型
  const econ = [...d.querySelectorAll('#hotelFilters .hpk-fchip')].find(e => e.textContent.includes('经济型'));
  click(econ);
  await sleep(20);
  check('筛选生效（结果变少）', cardCount() > 0 && cardCount() < 25, `cards=${cardCount()}`);
  // 回全部
  click([...d.querySelectorAll('#hotelFilters .hpk-fchip')].find(e => e.textContent.includes('全部档位')));
  await sleep(20);

  // 加载更多
  const moreBtn = d.querySelector('#hotelResults [data-act="hotel-more"]');
  check('有「加载更多」按钮', !!moreBtn);
  if (moreBtn) {
    click(moreBtn);
    await sleep(90);
    check('加载更多：追加下一页', cardCount() === 28, `cards=${cardCount()}`);
    check('加载更多：又调后端 1 次', counter.hotel === 2, `calls=${counter.hotel}`);
    check('返回不足一页 → 显示到底', /到底/.test(d.getElementById('hotelResults').textContent));
  }

  // 多图画廊
  const gwrap = d.querySelector('#hotelResults [data-act="hotel-gallery"]');
  check('多图卡片有画廊入口', !!gwrap);
  click(gwrap);
  await sleep(20);
  check('画廊浮层可打开', !!d.getElementById('hgOverlay'));

  // ---- 关键词搜索模式（与"推荐"是不同的代码路径：分页/hotelMode/hotelLastQuery）----
  d.getElementById('hotelQ').value = '钟楼';
  click(d.getElementById('hotelSearchBtn'));
  await sleep(90);
  check('关键词搜索：有新结果', cardCount() > 0, `cards=${cardCount()}`);
  check('关键词搜索：结果重置为一页', cardCount() === 25, `cards=${cardCount()}`);
  check('关键词搜索：后端再被调用', counter.hotel === 3, `calls=${counter.hotel}`);
  const more2 = d.querySelector('#hotelResults [data-act="hotel-more"]');
  check('搜索模式下仍可加载更多', !!more2);
  if (more2) {
    click(more2);
    await sleep(90);
    check('搜索模式：追加成功', cardCount() === 28, `cards=${cardCount()}`);
  }

  // ---- 「选这家」（此时还没规划过 → 走 pendingHotel 路径，不能调 /hotel/set）----
  const pickBtn = d.querySelector('#hotelResults [data-act="hotel-pick"]');
  const nameBefore = (d.getElementById('hotelText') || {}).textContent || '';
  check('选择前酒店行为默认文案', /未设置/.test(nameBefore), nameBefore.trim().slice(0, 12));
  click(pickBtn);
  await sleep(20);
  const nameAfter = (d.getElementById('hotelText') || {}).textContent || '';
  check('未规划时「选这家」写回酒店行', /测试酒店/.test(nameAfter), nameAfter.trim().slice(0, 16));
  check('未规划时选酒店**不调** /hotel/set（只暂存）', counter.set === 0, `set_calls=${counter.set}`);
  check('选完后弹层关闭', !d.getElementById('hotelModal'));

  // ---- 长图预览 ----
  d.getElementById('go').click();          // 走一次规划，让页面内部 plan 非空
  await sleep(80);
  let dl = 0;
  window.drawPlanCanvas = () => ({
    toDataURL: () => 'data:image/png;base64,AAAA',
    toBlob: cb => { dl++; cb({}); },
  });
  window.shareImage();
  await sleep(30);
  check('长图：先弹预览浮层', !!d.getElementById('shareModal'));
  check('长图：预览时没有自动下载', dl === 0, `downloads=${dl}`);
  const dlBtn = d.querySelector('#shareModal [data-act="share-download"]');
  check('长图：有「下载」按钮', !!dlBtn);
  click(dlBtn);
  await sleep(20);
  check('长图：点「下载」才真正导出', dl === 1, `downloads=${dl}`);

  // ---- 地图视图（新的 7 色相配色走这条渲染路径）----
  window.switchView('map');
  await sleep(40);
  check('地图视图可渲染（新配色路径）',
    d.getElementById('mapView').style.display === 'block' && !!d.getElementById('legend').textContent.trim());
  window.switchView('itin');
  await sleep(20);
  check('切回行程视图正常', d.getElementById('view').style.display === 'block');

  console.log('');
  if (errors.length) { console.log('运行时错误（' + errors.length + '）：'); errors.forEach(e => console.log('  ! ' + e)); }
  const bad = results.filter(r => !r.ok).length;
  console.log(`${results.length - bad}/${results.length} 项通过` + (errors.length ? `，${errors.length} 条运行时错误` : '，无运行时错误'));
  process.exit(bad || errors.length ? 1 : 0);
})();
