#!/usr/bin/env node
/*
 * 首页 hero 插画「动效是否肉眼可辨」门禁。
 *
 * 为什么需要这道门（真实事故，2026-09-28）：
 *   首页大图被反馈「变不成动态的」，前后改了 5 次都没解决。根因不是动画没写——
 *   动画一直在跑、时长也正常，而是**幅度太小**：全卡可见变化像素只有 2.6%/0.8s，
 *   肉眼直接读成一张静态插画。这类问题静态检查、单元测试、DOM 断言全都抓不到，
 *   只有「真渲染 + 比像素」能量出来。所以把它固化成一道门。
 *
 * 计量方法（自校验，防「显示绿但没在量」）：
 *   1) 对照组：注入 `animation:none`，同一时间窗测两次 —— 必须 ≈ 0%。
 *      这一条是关键：它证明这把尺子量的是「运动」，而不是噪声/闪烁/字体抖动。
 *   2) 实验组：正常页面，覆盖一整个动画周期逐窗采样，取均值。
 *   3) 断言 均值 >= 阈值。阈值 4%（实测 6.0% 留出余量；旧版 2.6% 会被判红）。
 *
 * 为什么只在本地跑、不进 CI：CI 是 ubuntu 无真实浏览器；装 Chromium 又慢又脆。
 * 与 tools/contrast_runtime.js 同理，留作本地真渲染复核。
 *
 * 用法：
 *   NODE_PATH=tools/node_modules node tools/hero_motion_check.js
 * 前置：本机 8000 端口在跑（uvicorn main:app）。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BASE = process.env.APP_URL || 'http://127.0.0.1:8000';
const THRESHOLD_PCT = 4.0;   // 均值下限
const CONTROL_MAX_PCT = 1.0; // 对照组上限（冻结后应≈0）
const WINDOWS = 8;           // 覆盖一个周期：8 × 0.8s = 6.4s
const WINDOW_MS = 800;

function findBrowser() {
  const cands = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  ];
  return cands.find(p => fs.existsSync(p));
}

async function main() {
  const exe = findBrowser();
  if (!exe) {
    console.log('SKIP: 未找到 Edge / Chrome，本门禁需要真实浏览器（CI 不跑这道）。');
    process.exit(0);
  }
  let chromium;
  try {
    ({ chromium } = require('playwright-core'));
  } catch (e) {
    console.log('SKIP: 缺少 playwright-core（npm install playwright-core --no-save --prefix tools）。');
    process.exit(0);
  }

  const browser = await chromium.launch({ executablePath: exe, args: ['--no-proxy-server'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 950 } });

  try {
    await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  } catch (e) {
    await browser.close();
    console.error(`✗ 打不开 ${BASE}/ —— 请先在 backend 下起服务：uvicorn main:app --host 127.0.0.1 --port 8000`);
    process.exit(1);
  }

  const card = page.locator('.hero-card');
  if (await card.count() === 0) {
    await browser.close();
    console.error('✗ 页面上找不到 .hero-card —— hero 插画的结构可能被改动了。');
    process.exit(1);
  }

  const shoot = async () => (await card.screenshot()).toString('base64');

  // 在浏览器里算像素差：免掉 Node 侧的 PNG 解码依赖
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

  const sample = async () => {
    await page.waitForTimeout(WINDOW_MS);
    const cur = await shoot();
    const p = await pctDiff(prev, cur);
    prev = cur;
    return p;
  };

  await page.waitForTimeout(900);
  let prev = await shoot();

  // ---- 对照组：冻结全部动画，应≈0 ----
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
  await page.waitForTimeout(300);
  prev = await shoot();
  const control = [];
  for (let i = 0; i < 3; i++) control.push(await sample());
  const controlMean = control.reduce((a, b) => a + b, 0) / control.length;

  // ---- 实验组：正常页面 ----
  await page.reload({ waitUntil: 'load' });
  await page.waitForTimeout(900);
  prev = await shoot();
  const vals = [];
  for (let i = 0; i < WINDOWS; i++) vals.push(await sample());
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const peak = Math.max(...vals);

  await browser.close();

  console.log('hero 动效可见度门禁（真渲染逐窗像素差，窗口 ' + (WINDOW_MS / 1000) + 's）');
  console.log('  对照组（冻结动画） 均值 ' + controlMean.toFixed(2) + '%   ← 必须 ≈ 0，否则这把尺子在量噪声');
  console.log('  逐窗变化率          ' + vals.map(v => v.toFixed(2) + '%').join('  '));
  console.log('  实验组             均值 ' + mean.toFixed(2) + '%   峰值 ' + peak.toFixed(2) + '%   阈值 ≥' + THRESHOLD_PCT + '%');

  const problems = [];
  if (controlMean > CONTROL_MAX_PCT) {
    problems.push(`对照组变化 ${controlMean.toFixed(2)}% > ${CONTROL_MAX_PCT}% —— 冻结后仍在变，说明测的不是动画（可能是加载态/字体/光标），本门禁失去意义`);
  }
  if (mean < THRESHOLD_PCT) {
    problems.push(`hero 动效可见度不足：均值 ${mean.toFixed(2)}% < ${THRESHOLD_PCT}%（参考：2026-09-28 被反馈「变不成动态」的那版是 2.61%）`);
  }
  if (problems.length) {
    problems.forEach(p => console.error('  ✗ ' + p));
    process.exit(1);
  }
  console.log('✅ hero 动效可见度过关');
}

main().catch(e => { console.error('✗ 门禁异常：', e && e.message); process.exit(1); });
