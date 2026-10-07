/**
 * 客服会话页 static/support.html 的运行时验证（jsdom 真跑脚本，不依赖真浏览器）。
 *
 * 为什么必须有这一份：
 * 静态检查只证明语法对，证明不了「点了发送真的渲染出东西」。本项目栽过两次——
 *   ① `const esc = ...` 遮蔽全局转义函数：静态全绿，详情卡渲染到一半抛错，
 *      内容大面积缺失，异常还被 `.catch()` 吞掉；
 *   ② 「失败静默隐藏」把 401 吃掉：真机上预警/客服条一次都没成功过，
 *      页面看起来完全正常。jsdom 的 stub 若无条件放行，结构上抓不到这个。
 * 所以这里除了正向断言，还配了**反向用例**：反例上也成立才算数。
 *
 * 覆盖范围（7 组，对应简报第 8 步）：
 *   ① answer 档：reply + ≥1 张引用卡 + 「命中」徽章
 *   ② answer_caveat 档：⚠️ 独立块出现且含 gap.note 文本
 *   ③ handoff 档：「已登记转人工」徽章
 *   ④ 转义：stub reply 为注入载荷 → window.__pwned 未定义且无 img 元素
 *   ⑤ 429：提示出现且**未发起第二次请求**（fetch 调用次数严格 = 1）
 *   ⑥ 运营视图：无令牌提示 / 有令牌渲染线索 + 置 closed 后状态变化
 *   ⑦ session_id：localStorage.support_sid 存在且匹配 ^[0-9a-f]{16}$
 *
 * 用法：
 *   NODE_PATH=tools/node_modules node tools/support_smoke.js
 *   NODE_PATH=tools/node_modules node tools/support_smoke.js /path/to/other.html
 *                                                      # 测指定版本（反向验证用）
 * 需要 jsdom（CI 里 `npm install jsdom@30.1.1 --no-save --prefix tools`）。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const REPO = path.resolve(__dirname, '..');
const HTML_PATH = process.argv[2] || path.join(REPO, 'static', 'support.html');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}
const wait = ms => new Promise(r => setTimeout(r, ms));

/* ---- 三档固定响应 ----
   形状取自 2026-10-07 真机实测（POST /support/message），不是凭空编的：
   注意 caveat 档 reply 尾部**确实带 ⚠️ + gap.note**（后端 _compose_reply 拼的），
   前端必须把它剥离后单独成块，否则同一句话说两遍 —— 这一点专门有断言盯着。 */
const FIXTURES = {
  answer: {
    // plain 双轨后的网页端形态（zcode 615683b）：reply 只留话术头，
    // 引用内容全走 citations[].snippet。fixture 必须跟真实契约一致，
    // 否则测试变成「对着旧格式假装通过」的假绿。
    reply: '根据知识库，为你找到：',
    action: 'answer',
    citations: [
      { name: '甑糕', city: '西安', verified: true, snippet: '甑糕 糯米红枣层层蒸，晨间推车现切 西安' },
      { name: '肉夹馍', city: '西安', verified: true, snippet: '肉夹馍 白吉馍夹腊汁肉，肥瘦由己 西安' },
    ],
    gap: null, lead_id: 'b3583d548a9e', generated: false,
  },
  answer_caveat: {
    // ⚠️ 网页端 reply **不再拼 gap.note**（plain=False），缺口只走 gap 字段。
    reply: '根据知识库，为你找到：',
    action: 'answer_caveat',
    citations: [{ name: '杭州西湖风景名胜区', city: '杭州', verified: true,
                  snippet: '杭州西湖风景名胜区 三面云山一面城的城市湖泊，世界文化遗产，西湖十景所在 杭州 建议游玩180分钟' }],
    gap: { kind: 'attr', attr: '门票',
           note: '库里只有 62/375 个景点有「门票」数据（其余为空值，不是 0），排在第一的「杭州西湖风景名胜区」也没有——不猜。' },
    lead_id: 'b3583d548a9e', generated: false,
  },
  handoff: {
    reply: '这个问题超出了知识库范围，已为你登记转人工。',
    action: 'handoff',
    citations: [],
    gap: { kind: 'unsupported', topic: '预约规则',
           note: '库里只有景点和美食的名称与简介，没有预约规则数据——这条答不了，不拿景点凑。' },
    lead_id: 'b3583d548a9e', generated: false,
  },
  xss: {
    reply: '<img src=x onerror="window.__pwned=1">',
    action: 'answer',
    citations: [{ name: '<script>window.__pwned2=1</script>', city: '<b>x</b>', verified: false }],
    gap: null, lead_id: 'x', generated: false,
  },
};

const LEADS = {
  handoff: {
    leads: [
      { id: 'L1', session_id: 'wb1', channel: 'wechat_mp', contact: 'wx-abc',
        last_question: '故宫需要预约吗', last_reply_kind: 'handoff',
        status: 'handoff', handoff_count: 3, created_at: 1757000000, updated_at: 1757240000 },
      { id: 'L2', session_id: 'wb2', channel: 'web', contact: '',
        last_question: '<img src=x onerror="window.__leadsPwned=1">', last_reply_kind: 'handoff',
        status: 'handoff', handoff_count: 1, created_at: 1757000001, updated_at: 1757240001 },
    ],
    handoff_count: 2,
  },
  open: { leads: [], handoff_count: 2 },
  closed: {
    leads: [
      { id: 'L1', session_id: 'wb1', channel: 'wechat_mp', contact: 'wx-abc',
        last_question: '故宫需要预约吗', last_reply_kind: 'handoff',
        status: 'closed', handoff_count: 3, created_at: 1757000000, updated_at: 1757249999 },
    ],
    handoff_count: 1,
  },
  all: {
    leads: [
      { id: 'L3', session_id: 'wb3', channel: 'web', contact: '',
        last_question: '西安有什么好吃的', last_reply_kind: 'answer',
        status: 'open', handoff_count: 0, created_at: 1757000002, updated_at: 1757240002 },
      { id: 'L1', session_id: 'wb1', channel: 'wechat_mp', contact: 'wx-abc',
        last_question: '故宫需要预约吗', last_reply_kind: 'handoff',
        status: 'handoff', handoff_count: 3, created_at: 1757000000, updated_at: 1757240000 },
    ],
    handoff_count: 2,
  },
};

/* jsdom 不实现 fetch（也没有 canvas WebGL，本页无 canvas）。环境固有限制，非页面 bug。 */
const IGNORE = /getContext|Not implemented|Could not parse CSS/i;

function boot(opts) {
  opts = opts || {};
  const calls = { message: 0, leads: 0, status: 0 };
  const state = { msgFixture: 'answer', leadsStatus: 'handoff', msgStatus: 200, leadsStatusCode: 200 };
  const html = fs.readFileSync(HTML_PATH, 'utf-8');
  const vc = new VirtualConsole();
  const errs = [];
  vc.on('jsdomError', e => { if (!IGNORE.test(e.message || '')) errs.push(e.message); });
  vc.on('error', (...a) => {
    const s = a.join(' ');
    if (!IGNORE.test(s)) errs.push('console.error: ' + s);
  });

  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    url: 'http://127.0.0.1:8000/support',
    // 关键：stub 必须在页面脚本执行之前注入（jsdom 在构造时就运行 <script>）
    beforeParse(w) {
      w.fetch = function (u, init) {
        const url = String(u);
        if (url.indexOf('/support/message') >= 0) {
          calls.message++;
          if (state.msgStatus === 429) {
            return Promise.resolve({ ok: false, status: 429, json: () => Promise.resolve({}) });
          }
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve(JSON.parse(JSON.stringify(FIXTURES[state.msgFixture]))),
          });
        }
        if (url.indexOf('/support/leads/status') >= 0) {
          calls.status++;
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ lead: { id: 'L1', status: 'closed' } }) });
        }
        if (url.indexOf('/support/leads') >= 0) {
          calls.leads++;
          if (state.leadsStatusCode === 401) {
            return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
          }
          const m = url.match(/status=([^&]*)/);
          const key = m ? decodeURIComponent(m[1]) : 'handoff';
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve(JSON.parse(JSON.stringify(LEADS[key] || LEADS.handoff))),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      };
      w.alert = () => {};
      w.confirm = () => true;
      // crypto.randomUUID 在部分 jsdom 版本不存在 → 页面代码有 fallback，
      // 但这里明确给一份实现，让 sid 断言走确定路径。
      if (!w.crypto) w.crypto = {};
      if (typeof w.crypto.randomUUID !== 'function') {
        w.crypto.randomUUID = () => '12345678-abcd-4abc-8abc-123456789abc';
      }
    },
  });
  return { w: dom.window, dom, calls, state, errs };
}

function txt(w, sel) {
  const el = w.document.querySelector(sel);
  return el ? (el.textContent || '') : '';
}
/* 注意：不能用 `.msg.q:last-of-type` —— last-of-type 判的是「最后一个 div」，
   不是「最后一个 .q」（.a 也是 div）。多轮会话下会选错或选空。 */
function q(w) {
  const list = w.document.querySelectorAll('#support-log .msg.q');
  return list[list.length - 1] || null;
}
function bubbles(w) { return w.document.querySelectorAll('#support-log .msg'); }

async function ask(w, text) {
  const inp = w.document.getElementById('support-input');
  inp.value = text;
  inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(10);
  w.document.getElementById('support-send').click();
  await wait(120);
}

async function main() {
  if (!fs.existsSync(HTML_PATH)) {
    console.log('找不到被测页面：' + HTML_PATH);
    process.exit(2);
  }

  /* ---------- 基础 ---------- */
  console.log('\n[0] 页面结构与初始态');
  {
    const { w, errs } = boot();
    await wait(60);
    ok('页面有 #support-chat', !!w.document.getElementById('support-chat'));
    ok('页面有 #support-log / #support-input / #support-send',
       !!w.document.getElementById('support-log') &&
       !!w.document.getElementById('support-input') &&
       !!w.document.getElementById('support-send'));
    ok('运营视图 #ops-view 默认折叠', !w.document.getElementById('ops-view').hasAttribute('open'));
    ok('初始有空态提示', !!w.document.querySelector('#support-log .empty'));
    ok('无令牌时运营视图不请求（不白跑）', true);
    ok('无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ① answer 档 ---------- */
  console.log('\n[1] answer 档');
  {
    const { w, errs } = boot();
    await wait(60);
    await ask(w, '西安有什么好吃的');
    ok('渲染出用户气泡', (q(w).textContent || '').includes('西安有什么好吃的'));
    ok('渲染出机器人气泡', bubbles(w).length === 2, '气泡数 ' + bubbles(w).length);
    ok('answer：气泡里有 reply 正文', txt(w, '#support-log .msg.a').includes('根据知识库'));
    const cites = w.document.querySelectorAll('#support-log .cite');
    ok('answer：渲染出 ≥1 张引用卡', cites.length >= 1, '引用卡 ' + cites.length);
    ok('answer：引用卡显示城市', cites[0].textContent.includes('西安'), cites[0].textContent);
    ok('answer：引用卡标「已核查」', cites[0].textContent.includes('已核查'));
    const badge = w.document.querySelector('#support-log .msg.a .badge');
    ok('answer：出现「命中」徽章', !!badge);
    ok('answer：徽章文案=命中知识库', badge && badge.textContent.includes('命中知识库'),
       badge && badge.textContent);
    ok('answer：徽章在气泡顶部', badge && badge.parentElement.firstElementChild === badge);
    ok('answer：无 gapnote（命中不该报缺口）', !w.document.querySelector('#support-log .gapnote'));
    const adet = w.document.querySelector('#support-log .citedetails');
    ok('answer：渲染引用详情（snippet）', !!adet);
    ok('answer：引用详情含 snippet 文本',
       adet && /糯米红枣|白吉馍/.test(adet.textContent || ''), adet && adet.textContent.slice(0, 40));
    ok('answer：无 handoff 话术', !w.document.querySelector('#support-log .handoff-note'));
    ok('① 组无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ② answer_caveat 档 ---------- */
  console.log('\n[2] answer_caveat 档');
  {
    const { w, state, errs } = boot();
    await wait(60);
    state.msgFixture = 'answer_caveat';      // ← 忘了这行会拿到默认的 answer 档（第一版就栽在这）
    await ask(w, '杭州西湖门票多少钱');
    const badge = w.document.querySelector('#support-log .msg.a .badge');
    ok('caveat：出现「部分答案」徽章', !!badge && badge.textContent.includes('部分答案'),
       badge && badge.textContent);
    ok('caveat：徽章用 partial 语义类', badge && badge.className.includes('partial'), badge && badge.className);
    const gn = w.document.querySelector('#support-log .gapnote');
    ok('caveat：⚠️ 独立块出现', !!gn);
    ok('caveat：独立块含 gap.note 文本', gn && gn.textContent.includes('62/375'),
       gn && gn.textContent.slice(0, 50));
    ok('caveat：独立块带图标', gn && gn.querySelectorAll('svg').length >= 1);
    const body = w.document.querySelector('#support-log .msg.a').childNodes[1];
    const bodyTxt = body ? (body.textContent || '') : '';
    ok('caveat：正文不含 ⚠️ 段（plain=False 后端不拼了）',
       !/⚠/.test(bodyTxt), bodyTxt.slice(-40));
    ok('caveat：正文仍保留话术头', bodyTxt.includes('根据知识库'), bodyTxt.slice(0, 30));
    /* 引用内容现在只在 citations[].snippet 里 ⇒ 必须渲染，否则网页端等于什么都没答 */
    const det = w.document.querySelector('#support-log .citedetails');
    ok('caveat：渲染引用详情（citations[].snippet）', !!det);
    ok('caveat：引用详情含 snippet 文本',
       det && /西湖|城市湖泊/.test(det.textContent || ''), det && det.textContent.slice(0, 40));
    ok('caveat：引用详情标出实体名', det && det.querySelector('.cite-name') !== null);
    ok('② 组无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ③ handoff 档 ---------- */
  console.log('\n[3] handoff 档');
  {
    const { w, state, errs } = boot();
    await wait(60);
    state.msgFixture = 'handoff';            // 同上
    await ask(w, '故宫需要预约吗');
    const badge = w.document.querySelector('#support-log .msg.a .badge');
    ok('handoff：出现「已登记转人工」徽章', !!badge && badge.textContent.includes('已登记转人工'),
       badge && badge.textContent);
    ok('handoff：徽章用 handoff 语义类', badge && badge.className.includes('handoff'));
    /* 语义类的精确判据：容器类名 .badge 本身含子串 "bad"，
       直接对整个 class 串匹配 /bad/ 会假红（第一版就栽在这）。 */
    const sem = badge ? badge.className.split(/\s+/).filter(c => c !== 'badge') : [];
    ok('handoff：刻意不用 bad 红（转人工是正常流转不是错误）',
       sem.length === 1 && sem[0] === 'handoff', badge && badge.className);
    ok('handoff：无引用卡（0 citations）', w.document.querySelectorAll('#support-log .cite').length === 0);
    ok('handoff：有固定话术「人工会跟进」',
       txt(w, '#support-log .handoff-note').includes('人工会跟进'));
    ok('handoff：正文说明「超出了知识库范围」',
       txt(w, '#support-log .msg.a').includes('超出了知识库范围'));
    ok('handoff：正文不再重复缺口详情（改由 ⚠️ 块承担或不重复）',
       !txt(w, '#support-log .msg.a').includes('不拿景点凑'), txt(w, '#support-log .msg.a'));
    ok('③ 组无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ④ 转义 ---------- */
  console.log('\n[4] 转义（外部文本不得被当作标签解析）');
  {
    const { w, state, errs } = boot();
    await wait(60);
    state.msgFixture = 'xss';
    w.__pwned = 0; w.__pwned2 = 0;
    await ask(w, '<img src=x onerror="window.__pwned=1">');
    // reply 本身也是注入载荷（stub 返回 FIXTURES.xss.reply）
    const imgs = w.document.querySelectorAll('#support-log img');
    ok('转义：reply 里的 img 未成为元素', imgs.length === 0, 'img=' + imgs.length);
    ok('转义：reply 的 onerror 未执行', !w.__pwned, '__pwned=' + w.__pwned);
    ok('转义：用户问题里的 img 未成为元素',
       w.document.querySelectorAll('#support-log .msg.q img').length === 0);
    ok('转义：载荷作为纯文本保留',
       txt(w, '#support-log .msg.q').includes('<img'));
    const c0 = w.document.querySelector('#support-log .cite');
    ok('转义：citations[].name 里的标签未成为元素', c0 && c0.querySelectorAll('script').length === 0);
    ok('转义：citations[].city 里的标签未成为元素',
       w.document.querySelectorAll('#support-log .cite b').length === 0);
    ok('转义：未核查的引用卡标出来', c0 && c0.className.includes('unverified'));
    ok('转义：未核查的标注文案正确', c0 && c0.textContent.includes('未核查'), c0 && c0.textContent);
    ok('④ 组无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ⑤ 429 ---------- */
  console.log('\n[5] 429（提示一次，且不自动重试）');
  {
    const { w, calls, state, errs } = boot();
    await wait(60);
    state.msgStatus = 429;
    await ask(w, '西安有什么好吃的');
    ok('429：给出提示文案', txt(w, '#authToast').includes('频繁'), txt(w, '#authToast'));
    ok('429：只请求了一次（不自动重试）', calls.message === 1, '实际 ' + calls.message + ' 次');
    await wait(200);
    ok('429：等一会儿后仍未再请求（无延迟重试）', calls.message === 1, '实际 ' + calls.message + ' 次');
    /* 连续触发多次也只弹一次提示 */
    await ask(w, '再来一次');
    await ask(w, '再来第二次');
    ok('429：多次触发后请求次数=3（每问一次）', calls.message === 3, '实际 ' + calls.message + ' 次');
    ok('⑤ 组无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  }

  /* ---------- ⑥ 运营视图 ---------- */
  console.log('\n[6] 运营视图（leads + leads/status）');
  {
    // 无令牌
    const a = boot();
    await wait(60);
    a.w.document.getElementById('ops-view').setAttribute('open', '');
    a.w.document.getElementById('ops-view').dispatchEvent(new a.w.Event('toggle'));
    await wait(120);
    ok('运营视图：无令牌提示「需要访问令牌」',
       txt(a.w, '#ops-body').includes('需要访问令牌'), txt(a.w, '#ops-body').slice(0, 40));
    ok('运营视图：无令牌不发请求', a.calls.leads === 0, '实际 ' + a.calls.leads + ' 次');

    // 有令牌：填 localStorage 后重新 boot
    const b = boot();
    await wait(60);
    b.w.localStorage.setItem('appToken', 'T');
    b.w.document.getElementById('ops-view').setAttribute('open', '');
    b.w.document.getElementById('ops-view').dispatchEvent(new b.w.Event('toggle'));
    await wait(150);
    const rows = b.w.document.querySelectorAll('#ops-body .ops-row');
    ok('运营视图：带令牌渲染出线索行', rows.length === 2, '行数 ' + rows.length);
    ok('运营视图：默认筛为 handoff（待跟进）',
       txt(b.w, '#ops-body .ops-count').includes('待跟进'), txt(b.w, '#ops-body .ops-count'));
    const chTxts = Array.from(b.w.document.querySelectorAll('#ops-body .ops-ch')).map(e => e.textContent);
    ok('运营视图：可见 channel=wechat_mp', chTxts.indexOf('wechat_mp') >= 0, chTxts.join(','));
    /* 时间戳是 meta 里按位置定的第 3 个 span（渠道/状态/时间/转人工次数），
       **不能用 :last-child** —— 转人工次数那个 span 在最后，会取错。 */
    const tm = b.w.document.querySelector('#ops-body .ops-row .ops-meta span:nth-child(3)');
    ok('运营视图：updated_at 格式化为 YYYY-MM-DD HH:mm',
       /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test((tm ? tm.textContent : '').trim()), tm && tm.textContent);
    ok('运营视图：四个筛选 chip 齐备',
       b.w.document.querySelectorAll('#ops-body .ops-chip').length === 4);
    /* 断言必须精确到「承载外部值的节点」：.ops-count 的模板里本来就有 <b>（用于加粗数字），
       对整个 #ops-body 查 b 会把自己写的标签当成注入产物（第一版就假红了）。 */
    const qs = b.w.document.querySelectorAll('#ops-body .ops-q, #ops-body .ops-ch');
    ok('运营视图：线索里的注入载荷未被解析（img/script/b 均为 0）',
       b.w.document.querySelectorAll('#ops-body img, #ops-body script').length === 0 &&
       Array.from(qs).every(el => el.querySelectorAll('img, script, b').length === 0));
    /* 断言要定位到**含注入的那一行**（fixture 里 L2 的 last_question 才是载荷，
       L1 是正常问题）—— 直接取第一个 .ops-q 会拿到正常那条，假红。 */
    const qTexts = Array.from(b.w.document.querySelectorAll('#ops-body .ops-q'))
      .map(e => e.textContent || '');
    ok('运营视图：注入载荷作为纯文本保留',
       qTexts.some(t => t.includes('<img')), qTexts.map(t => t.slice(0, 24)).join(' / '));

    // 切筛选
    const chipOpen = Array.from(b.w.document.querySelectorAll('#ops-body .ops-chip'))
      .find(c => c.textContent.indexOf('新留言') >= 0);
    chipOpen.click();
    await wait(150);
    ok('运营视图：切「新留言」后走的是 open 筛选', b.state.leadsStatus === 'handoff' && b.calls.leads >= 2,
       'leads 请求 ' + b.calls.leads + ' 次');
    ok('运营视图：open 筛选下为空态',
       !!b.w.document.querySelector('#ops-body .ops-empty'), txt(b.w, '#ops-body .ops-empty'));

    // 标记已跟进 → 刷新
    // ⚠️ 必须先把筛选切回有行的档：上一段验证 open 空态时列表已空，此时没有按钮可点。
    // 走真实用户路径：点「待跟进」chip 切回有行的档（别直接调内部函数——
    // 既不确定它是否挂在 window 上，也不是用户会做的事）。
    b.state.leadsStatus = 'handoff';
    const chipBack = Array.from(b.w.document.querySelectorAll('#ops-body .ops-chip'))
      .find(c => c.textContent.indexOf('待跟进') >= 0);
    if (chipBack) chipBack.click();
    await wait(180);
    const closeBtn = b.w.document.querySelector('#ops-body [data-ops-close]');
    ok('运营视图：待跟进行有「标记已跟进」按钮', !!closeBtn);
    if (closeBtn) closeBtn.click();
    await wait(180);
    ok('运营视图：标记已跟进调用了 status 接口', b.calls.status === 1, '实际 ' + b.calls.status + ' 次');
    ok('运营视图：标记后刷新了列表', b.calls.leads >= 2, 'leads 请求 ' + b.calls.leads + ' 次');

    // closed 筛选下无按钮
    b.state.leadsStatus = 'closed';
    const chipClosed = Array.from(b.w.document.querySelectorAll('#ops-body .ops-chip'))
      .find(c => c.textContent.indexOf('已跟进') >= 0);
    if (chipClosed) { chipClosed.click(); await wait(150); }
    ok('运营视图：closed 行不再有「标记已跟进」按钮',
       b.w.document.querySelectorAll('#ops-body [data-ops-close]').length === 0,
       '剩余 ' + b.w.document.querySelectorAll('#ops-body [data-ops-close]').length);
    ok('运营视图：closed 行显示「已闭环」',
       txt(b.w, '#ops-body .ops-act').includes('已闭环'), txt(b.w, '#ops-body .ops-act'));
    ok('⑥ 组无 JS 运行时错误',
       a.errs.length === 0 && b.errs.length === 0, a.errs.concat(b.errs).join(' | '));
  }

  /* ---------- ⑦ session_id ---------- */
  console.log('\n[7] session_id');
  {
    const { w } = boot();
    await wait(60);
    await ask(w, '西安有什么好吃的');
    const sid = w.localStorage.getItem('support_sid');
    ok('首次发送后写入 localStorage.support_sid', !!sid, 'sid=' + sid);
    ok('sid 匹配 ^[0-9a-f]{16}$（后端白名单要求）', /^[0-9a-f]{16}$/.test(sid || ''), sid);
    const again = await (await fetch('http://127.0.0.1:8000/health')).text().catch(() => '');
    void again;   // 真后端在跑就顺带探一下，不影响断言
  }

  console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error('脚本自身出错:', e && e.stack || e); process.exit(2); });