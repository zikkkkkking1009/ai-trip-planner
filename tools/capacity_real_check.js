/**
 * 规划页「容量预警」条（#capBar）的**真机**校验。
 *
 * 与 tools/frontend_smoke.js 的分工（互补，不重复）：
 *   - smoke（jsdom）：覆盖**逻辑分支**——装得下/装不下/接口失败静默/落选名转义。
 *   - 本脚本（真机）：覆盖 **jsdom 根本跑不了的东西**——
 *     ① 真实 CSS 布局：chip 有没有高度、有没有横向溢出、警示底色与文字是否真压得住；
 *     ② 真实令牌注入：/plan/capacity 受 verify_token 保护，页面必须已带上 X-App-Token，
 *        否则真机上只会静默隐藏（jsdom 的 stub 无条件放行，抓不到这个错）；
 *     ③ 真实交互：点预置卡片 → 预警条自己冒出来。
 *
 * 为什么非要有真机这一层：本项目在 hero 动效上栽过一次（jsdom 全绿、真机动画不动）；
 * 而「静默隐藏」这个兜底分支更阴 —— 令牌没带上时它照样隐藏，页面看着「正常」，
 * 实际上预警从来没工作过。这类错只有打真后端才暴露。
 *
 * 用法：NODE_PATH=tools/node_modules node tools/capacity_real_check.js
 * 未找到 Edge/Chrome、或本地服务没起来时打印 SKIP 并 exit 0（与 home_ask_real_check 一致，CI 不依赖真机）。
 */
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');
const fs = require('fs');

const BASE = process.env.APP_BASE || 'http://127.0.0.1:8000';
const EXE = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find(p => fs.existsSync(p));

/* 受保护接口在开了 APP_TOKEN 的部署上会 401。本脚本必须自己把令牌带上，
   否则「失败静默隐藏」会把 401 吃掉 —— 页面看着正常，预警其实一次都没成功过。
   这不是绕过鉴权，是复刻真实用户行为：用户也会在页面右上角填同一个令牌。
   令牌只从本机 backend/.env 读（该文件在 .gitignore 内），不打印、不外传。 */
function localToken() {
  if (process.env.APP_TOKEN) return process.env.APP_TOKEN;
  const envPath = path.resolve(__dirname, '..', 'backend', '.env');
  try {
    const m = fs.readFileSync(envPath, 'utf8').match(/^\s*APP_TOKEN\s*=\s*(.+?)\s*$/m);
    return m ? m[1].replace(/^["']|["']$/g, '') : '';
  } catch { return ''; }
}

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
};

async function alive() {
  try {
    const r = await fetch(BASE + '/health', { signal: AbortSignal.timeout(3000) });
    return r.ok;
  } catch { return false; }
}

async function main() {
  if (!EXE) { console.log('SKIP: 未找到 Edge/Chrome（本门禁需要真实浏览器）。'); process.exit(0); }
  if (!(await alive())) { console.log('SKIP: ' + BASE + ' 没有服务在跑（先启动后端）。'); process.exit(0); }

  const browser = await chromium.launch({ executablePath: EXE, args: ['--no-proxy-server'] });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 950 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e.message)));

  // 记录 capacity 请求：既要确认**发了**，也要看**带没带令牌**。
  const capCalls = [];
  page.on('request', r => {
    if (r.url().includes('/plan/capacity')) {
      capCalls.push({ token: (r.headers() || {})['x-app-token'] || null });
    }
  });

  try {
    /* 令牌必须在 goto **之前**用 addInitScript 注入 localStorage。
       否则首屏那些受保护请求（/favorites 等）在新 profile 里是裸的 → 401，
       pageerror 里会挂一条 "HTTP 401"，而它与本脚本要测的东西无关，
       却会让「无 JS 运行时错误」永远红 —— 那就是自己制造的假红。
       （首轮实测就是踩了这个：401 来自 GET /favorites，不是 /plan/capacity。） */
    const tok = localToken();
    if (tok) {
      await ctx.addInitScript(t => {
        // 键名取自 index.html:1231（var KEY = 'appToken'）—— 别猜，猜错注入等于没注入
        try { localStorage.setItem('appToken', t); } catch (e) {}
      }, tok);
    }

    await page.goto(BASE + '/app', { waitUntil: 'load', timeout: 60000 });
    await page.waitForTimeout(1500);

    // 令牌键名以页面实际使用的为准（app_token）；若猜错则这里补设一次，
    // 保证 /plan/capacity 至少能通（下面的令牌断言会验证它到底带没带上）。
    if (tok) {
      await page.evaluate(t => { try { window.setAppToken(t); } catch (e) {} }, tok);
      await page.waitForTimeout(200);
    }

    const bar = page.locator('#capBar');
    ok('第②步有容量预警容器', (await bar.count()) === 1);

    // 走真实点击进第②步：填城市 → 点「下一步」。不直接调 setStep ——
    // 后者是 const 声明、不挂 window，且真机复核的意义本就是走用户路径。
    const cityInput = page.locator('#cityInput');
    if (!(await cityInput.inputValue())) {
      await cityInput.fill('西安');
      await page.waitForTimeout(700);   // 等预置景点按城市加载
    }
    await page.locator('#nextBtn').click();
    await page.waitForTimeout(700);
    ok('已进入第②步', await page.locator('#step2Panel').isVisible());

    /* 进第②步时 spots **已经是满的** —— loadDemoSpots() 会把本城预置直接灌进 spots
       （index.html:1686）。而「取消全部」在真机上不可达：预置卡是逐张舞台，
       取消一张就推进到下一张，没法一次清空。
       ⇒ 空状态（!spots.length 早退）这条**只**由 tools/frontend_smoke.js 读源码验，
       真机不硬凑一个页面走不到的状态（凑出来的红是假红，比没这条更糟）。
       真机只覆盖真实可达的主链路。 */
    ok('预置景点默认即为已选 → 预警条出现（产品既有行为）',
      (await page.locator('.pickcard:not(.off)').count()) > 0 && await bar.isVisible());

    // 走真实交互勾选：点预置卡片，预警条自己冒出来
    const cards = page.locator('.pickcard:not(.off)');
    const nCards = await cards.count();
    const nPick = Math.min(6, nCards);
    for (let i = 0; i < nPick; i++) {
      await cards.nth(0).click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(120);
    }
    await page.waitForTimeout(1400);   // 250ms 防抖 + 请求往返，留足余量

    ok('勾选后向 /plan/capacity 发了请求', capCalls.length > 0, '调用 ' + capCalls.length + ' 次');
    /* 令牌这条必须单列断言：接口 401 时 catch 会静默隐藏，预警条「不显示」——
       表面上和「还没触发」长得一模一样。这条断的是「以为测了、其实没测」，
       就是 2026-10-04 这次真机首跑报红的原因（后端开了 APP_TOKEN，浏览器没令牌）。
       对照 home_ask_real_check 的分工：那边只测公开 /ask，本脚本专测受保护接口。 */
    ok('capacity 请求带上了令牌（受保护接口，未鉴权会 401 然后被静默隐藏）',
      capCalls.length > 0 && capCalls.every(c => !!c.token),
      capCalls.length ? 'token=' + (capCalls[0].token ? '已带' : '缺失') : '无调用');
    // 静默隐藏的分支会掩盖「令牌没带上」这个错：没令牌 → 401 → catch → 隐藏。
    // 所以必须显式断言预警条**可见**，否则令牌/接口问题会被兜底吃掉。
    ok('预警条在真机上真的显示出来了（说明接口通、令牌对）', await bar.isVisible());
    if (await bar.isVisible()) {
      const txt = await bar.textContent();
      ok('预警文案非空且有实质内容', (txt || '').trim().length > 10, (txt || '').slice(0, 50));
      ok('文案用「可能 / 预计」等留有余地的措辞，未断言式',
        /可能|预计|装得下|排不下/.test(txt || ''), (txt || '').slice(0, 40));

      // 布局（jsdom 测不了）
      const box = await bar.boundingBox();
      ok('预警条有实际渲染高度', !!box && box.height > 20, box ? JSON.stringify(box) : 'null');
      ok('预警条未横向溢出视口', !!box && box.width <= 1280, box ? String(box.width) : 'null');

      // 真实计算样式：底色不能与卡片面同色（否则等于没渲染出来）
      const cs = await bar.evaluate(el => {
        const s = getComputedStyle(el);
        return { bg: s.backgroundColor, color: s.color, cls: el.className };
      });
      ok('预警条有实心底色（不是透明/与页面同色）',
        cs.bg && !/rgba\(0, 0, 0, 0\)/.test(cs.bg), JSON.stringify(cs));
    }

    ok('无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  } catch (e) {
    fail++;
    console.log('  ✗ 执行中断: ' + e.message);
    const shot = path.join(os.tmpdir(), 'capacity_real_fail.png');
    try { await page.screenshot({ path: shot, fullPage: false }); console.log('    （失败截图：' + shot + '）'); } catch { /* 忽略 */ }
  } finally {
    await browser.close();
  }

  console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error('真机校验异常:', e && e.stack || e); process.exit(2); });
