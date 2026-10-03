/**
 * 首页检索问答演示框的**真机**校验（真实浏览器 + 真实后端）。
 *
 * 与 tools/home_ask_smoke.js 的分工（两者互补，不是重复）：
 *   - smoke（jsdom）：覆盖**逻辑分支**——默认不烧配额、grounded 三态、降级、转义、冷却。
 *   - 本脚本（真机）：覆盖 **jsdom 根本跑不了的东西**——
 *     ① `IntersectionObserver` 懒加载（jsdom 没有 IO，演示框在那边走的是「无 IO」降级分支，
 *        真实的懒加载路径**只在这里被执行**）；
 *     ② 真实 CSS 布局（元素有没有高度、有没有溢出视口）。
 *
 * 为什么非要有真机这一层：本项目在 hero 动效上栽过一次——jsdom 全绿，真机上动画根本不动。
 * 新增交互 UI 一律补真机断言。
 *
 * 用法：NODE_PATH=tools/node_modules node tools/home_ask_real_check.js
 * 未找到 Edge/Chrome、或本地服务没起来时打印 SKIP 并 exit 0（与 hero_motion_check 一致，CI 不依赖真机）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const BASE = process.env.APP_BASE || 'http://127.0.0.1:8000';
const EXE = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find(p => fs.existsSync(p));

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

  try {
    await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });

    // 滚到演示框 —— 这一步就是 jsdom 覆盖不到的 IntersectionObserver 路径
    await page.locator('#ask').scrollIntoViewIfNeeded();
    await page.waitForTimeout(900);

    const hint0 = await page.locator('#askOut').textContent();
    ok('懒加载已触发（提示已从占位文案切换）',
       !/滚动到此处才建立索引/.test(hint0 || ''), hint0);

    const chips = page.locator('.askchip');
    const nChips = await chips.count();
    ok('示例 chip 已渲染', nChips > 0, '数量 ' + nChips);
    if (!nChips) throw new Error('没有示例 chip，后续用例无法运行');

    // ① 纯检索：零 LLM 配额
    await chips.first().click();
    await page.waitForSelector('.askitem', { timeout: 15000 });
    ok('真机渲染出检索条目', (await page.locator('.askitem').count()) > 0);
    ok('纯检索不出现生成答案区', (await page.locator('.askans').count()) === 0);
    ok('结果带对齐标记', /已对齐|未对齐/.test(await page.locator('#askOut').textContent() || ''));

    // ② 勾选生成：真实 LLM 调用（1 次）
    await page.waitForTimeout(3200);          // 绕开前端 3 秒冷却
    await page.locator('#askGen').check();
    await chips.nth(1).click();
    await page.waitForSelector('.askans', { timeout: 40000 });
    const ansTxt = await page.locator('.askans .txt').textContent();
    ok('生成答案区有正文', !!ansTxt && ansTxt.trim().length > 0, (ansTxt || '').slice(0, 40));
    const gate = page.locator('.askgate');
    ok('grounded 徽章出现', (await gate.count()) > 0);
    ok('徽章给出明确结论',
       /已通过|未落地|未核查/.test((await gate.count()) ? await gate.first().textContent() : ''));

    // ③ 布局（jsdom 测不了）
    const box = await page.locator('.askbox').boundingBox();
    ok('演示框有实际渲染高度', !!box && box.height > 120, box ? JSON.stringify(box) : 'null');
    ok('演示框未溢出视口宽度', !!box && box.width <= 1280, box ? String(box.width) : 'null');

    ok('无 JS 运行时错误', errs.length === 0, errs.join(' | '));
  } catch (e) {
    fail++;
    console.log('  ✗ 执行中断: ' + e.message);
    const shot = path.join(os.tmpdir(), 'home_ask_real_fail.png');
    try { await page.screenshot({ path: shot }); console.log('    （失败截图：' + shot + '）'); } catch { /* 忽略 */ }
  } finally {
    await browser.close();
  }

  console.log('\n' + pass + '/' + (pass + fail) + ' 项通过\n');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error('脚本出错:', e); process.exit(1); });
