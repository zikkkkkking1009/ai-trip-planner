#!/usr/bin/env node
/*
 * 首页 hero 插画「动效是否肉眼可辨」门禁（本地，不进 CI）。
 *
 * 为什么需要这道门（真实事故，2026-09-28）：
 *   首页大图被反馈「变不成动态的」，前后改了 6 次都没解决。踩了两个坑，都不是"动画没写"：
 *     ① 幅度太小：全卡可见变化像素只有 2.6%/0.8s，肉眼读成静态插画；
 *     ② **环境**：站长的 Windows「动画效果」是关的（SPI_GETCLIENTAREAANIMATION = 0）
 *        ⇒ 浏览器恒报 prefers-reduced-motion:reduce ⇒ 当时的降级把 hero 冻成静态帧。
 *   静态检查、单元测试、DOM 断言全都抓不到这两类问题，只有「真渲染 + 比像素 + 换环境」能量出来。
 *
 * 所以本门禁断言三件事：
 *   1) 对照组：注入 animation:none 后必须 ≈ 0% —— 证明这把尺子量的是运动，不是噪声；
 *   2) 正常环境：hero 可见变化 ≥ 阈值；
 *   3) **reduce 环境**（显式传 reducedMotion:'reduce'）：也必须 ≥ 阈值。
 *      ③ 是这次事故的直接产物 —— hero 是**有意**豁免 reduce 的（动效是本卡产品诉求），
 *      谁把豁免改回去（比如又加一条 @media reduce 把它定住），③ 立刻变红。
 *
 * ⚠️ 关于 emulation（这条坑过一次，写下来）：
 *   Playwright 默认把 reducedMotion 模拟成 no-preference —— 也就是说「不传参数去测 reduce」
 *   永远测不到，会得出"用户环境没有 reduce"的错误结论。必须显式 { reducedMotion: 'reduce' }。
 *   同理：想读**操作系统真实**的 reduce 值不能用 Playwright，要读 SPI_GETCLIENTAREAANIMATION。
 *
 * 用法：NODE_PATH=tools/node_modules node tools/hero_motion_check.js
 * 前置：本机 8000 端口在跑（uvicorn main:app）。
 */
'use strict';
const fs = require('fs');

const BASE = process.env.APP_URL || 'http://127.0.0.1:8000';
const THRESHOLD_PCT = 4.0;   // 各环境下的均值下限
const CONTROL_MAX_PCT = 1.0; // 对照组上限（冻结后应 ≈0）
const WINDOWS = 8;           // 覆盖一个 6.4s 周期
const WINDOW_MS = 800;

function findBrowser() {
  return [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  ].find(p => fs.existsSync(p));
}

async function measure(chromium, exe, reducedMotion) {
  const browser = await chromium.launch({ executablePath: exe, args: ['--no-proxy-server'] });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 950 }, reducedMotion });
  const page = await ctx.newPage();
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  const card = page.locator('.hero-card');
  const shoot = async () => (await card.screenshot()).toString('base64');
  const pctDiff = (a, b) => page.evaluate(async ([a, b]) => {
    const load = src => new Promise(r => { const i = new Image(); i.onload = () => r(i); i.src = 'data:image/png;base64,' + src; });
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    const c = document.createElement('canvas'); c.width = ia.width; c.height = ia.height;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(ia, 0, 0); const da = g.getImageData(0, 0, c.width, c.height).data;
    g.clearRect(0, 0, c.width, c.height); g.drawImage(ib, 0, 0);
    const db = g.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < da.length; i += 4) {
      const d = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
      if (d > 8) n++;
    }
    return n / (da.length / 4) * 100;
  }, [a, b]);

  await page.waitForTimeout(900);
  let prev = await shoot();
  const sample = async () => { await page.waitForTimeout(WINDOW_MS); const cur = await shoot(); const v = await pctDiff(prev, cur); prev = cur; return v; };

  // 对照组：冻结全部动画。
  // ⚠️ 不能用注入 `*{animation:none!important}` 的做法 —— hero 的动画声明本身带了
  // !important（有意豁免 reduce，见 home.html），`*` 的特异性 0,0,0 压不过
  // `.hero-card svg .flow` 的 0,2,1，等于冻不住、对照组会假红。
  // 改用 Web Animations API 暂停：不受选择器特异性影响，确定性冻结。
  await page.evaluate(() => document.getAnimations().forEach(a => a.pause()));
  await page.waitForTimeout(300);
  prev = await shoot();
  const ctl = [];
  for (let i = 0; i < 3; i++) ctl.push(await sample());

  // 实验组：正常页面
  await page.reload({ waitUntil: 'load' });
  await page.waitForTimeout(900);
  prev = await shoot();
  const vals = [];
  for (let i = 0; i < WINDOWS; i++) vals.push(await sample());

  const info = await page.evaluate(() => {
    const g = (s, p) => { const e = document.querySelector(s); return e ? getComputedStyle(e)[p] : 'MISSING'; };
    return {
      self: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduce' : 'no-preference',
      flowDur: g('.hero-card svg .flow', 'animationDuration'),
      svgAnim: g('.hero-card svg', 'animationName'),
    };
  });

  await browser.close();
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  return { ctlMean: ctl.reduce((a, b) => a + b, 0) / ctl.length, mean, peak: Math.max(...vals), vals, info };
}

async function main() {
  const exe = findBrowser();
  if (!exe) { console.log('SKIP: 未找到 Edge / Chrome，本门禁需要真实浏览器（CI 不跑这道）。'); process.exit(0); }
  let chromium;
  try { ({ chromium } = require('playwright-core')); }
  catch { console.log('SKIP: 缺少 playwright-core。'); process.exit(0); }

  const problems = [];
  const rows = [];
  for (const mode of ['no-preference', 'reduce']) {
    const r = await measure(chromium, exe, mode);
    rows.push({ mode, ...r });
    console.log(`\n── 环境：${mode}（页面自报 ${r.info.self}）`);
    console.log(`   对照组（冻结动画）均值 ${r.ctlMean.toFixed(2)}%   ${r.ctlMean <= CONTROL_MAX_PCT ? '✓ 尺子有效' : '✗ 尺子在量噪声'}`);
    console.log(`   逐窗变化率 ${r.vals.map(v => v.toFixed(2) + '%').join('  ')}`);
    console.log(`   均值 ${r.mean.toFixed(2)}%  峰值 ${r.peak.toFixed(2)}%  阈值 ≥${THRESHOLD_PCT}%   .flow duration=${r.info.flowDur}`);
    if (r.ctlMean > CONTROL_MAX_PCT) problems.push(`${mode}：冻结后仍变化 ${r.ctlMean.toFixed(2)}% —— 这把尺子测的不是动画`);
    if (r.mean < THRESHOLD_PCT) {
      problems.push(`${mode}：hero 动效可见度不足，均值 ${r.mean.toFixed(2)}% < ${THRESHOLD_PCT}%`
        + (mode === 'reduce'
          ? '（hero 是有意豁免 reduce 的，别把豁免改回去）'
          : '（参考：2026-09-28 被反馈"变不成动态"那版是 2.61%）'));
    }
  }

  console.log('\n════════ 汇总 ════════');
  rows.forEach(r => console.log(`  ${r.mode.padEnd(14)} 均值 ${r.mean.toFixed(2)}%  峰值 ${r.peak.toFixed(2)}%  对照组 ${r.ctlMean.toFixed(2)}%`));
  if (problems.length) { problems.forEach(p => console.error('  ✗ ' + p)); process.exit(1); }
  console.log('✅ hero 动效在「正常」与「reduce」两种环境下都肉眼可辨');
}

main().catch(e => { console.error('✗ 门禁异常：', e && e.message); process.exit(1); });
