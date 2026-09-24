#!/usr/bin/env node
/**
 * 运行时对比度实测（本地工具，不进 CI）。
 *
 * 与 tools/check_contrast.py 的分工
 * --------------------------------
 * check_contrast.py —— 静态门禁：解析 :root 令牌，按「令牌 × 参照底」断言。
 *                     快、零依赖、进 CI；**看不到 color-mix() 等运行时动态色**。
 * 本文件           —— 运行时实测：用真实浏览器渲染，遍历页面上**每一个文字元素**，
 *                     读 computedStyle 的实际颜色/字号/字重，向上找最近的不透明背景，
 *                     按 WCAG 2.2 算。慢、要浏览器；但能抓到静态门禁的全部盲区。
 *
 * 为什么两者都要
 * --------------
 * 2026-09-24 首页 hero 副标题实测 3.64:1，而静态门禁报「通过」——
 * 因为它的颜色来自 `color-mix(in oklab, var(--accent-ink) 88%, var(--accent))`，
 * 是运行时算出来的，不落在任何令牌上。静态永远量不到动态值。
 *
 * 依赖
 * ----
 * playwright-core + 本机已安装的浏览器（开发机上的 Edge/Chrome 即可）。
 * 用法：
 *   node tools/contrast_runtime.js static/home.html
 *   node tools/contrast_runtime.js static/home.html static/index.html
 *   node tools/contrast_runtime.js static/home.html --dark       # 系统暗色优先
 *   node tools/contrast_runtime.js static/home.html --all         # 连达标项也列出
 *
 * 退出码：发现不达标项时为 1。
 */

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = path.resolve(__dirname, '..');

// 依次尝试本机浏览器（不下载 chromium，避免给 CI 增负担）
const BROWSER_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
];

function findBrowser() {
  for (const p of BROWSER_CANDIDATES) {
    try { if (fs.statSync(p).isFile()) return p; } catch (e) { /* 继续找 */ }
  }
  return null;
}

function loadPlaywright() {
  const tries = ['playwright-core', 'playwright',
    path.join(ROOT, 'tools', 'node_modules', 'playwright-core')];
  for (const t of tries) {
    try { return require(t); } catch (e) { /* 继续找 */ }
  }
  console.error('✗ 找不到 playwright-core。请先执行：');
  console.error('    npm install playwright-core --no-save --prefix tools');
  process.exit(2);
}

// 在页面里跑：遍历所有文字元素，输出实测结果
const COLLECT = function () {
  const cvs = document.createElement('canvas');
  cvs.width = cvs.height = 1;
  const c2d = cvs.getContext('2d', { willReadFrequently: true });

  // 用 canvas 规范化任意 CSS 颜色（rgb / oklch / color-mix / color()）。
  // 新版浏览器对 CSS Color 4 的 computed value 不再统一输出 rgb()，字符串解析会漏。
  const parse = (color) => {
    if (!color || color === 'none') return null;
    c2d.clearRect(0, 0, 1, 1);
    c2d.fillStyle = 'rgba(0,0,0,0)';
    c2d.fillStyle = color;
    if (c2d.fillStyle === 'rgba(0, 0, 0, 0)') return null;
    c2d.fillRect(0, 0, 1, 1);
    const d = c2d.getImageData(0, 0, 1, 1).data;
    return { r: d[0], g: d[1], b: d[2], a: d[3] / 255 };
  };
  const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const ratio = (a, b) => { const hi = Math.max(a, b), lo = Math.min(a, b); return (hi + 0.05) / (lo + 0.05); };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
  });
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a >= 0.99) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const desc = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
    if (cls.length) s += '.' + cls.slice(0, 3).join('.');
    return s;
  };
  const textOf = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.trim().replace(/\s+/g, ' ').slice(0, 34);
  };

  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.1) continue;
    const isSvgText = el.tagName.toLowerCase() === 'text' || el.tagName.toLowerCase() === 'tspan';
    const txt = isSvgText ? (el.textContent || '').trim().slice(0, 34) : textOf(el);
    if (!txt) continue;

    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;

    const bg = bgOf(el);
    const fgRaw = parse(isSvgText ? cs.fill : cs.color);
    if (!fgRaw) continue;
    const fg = fgRaw.a >= 0.99 ? fgRaw : over(fgRaw, bg);

    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10) || 400;
    // WCAG 2.2：大字号 = ≥24px，或 ≥18.66px 且加粗
    const isLarge = size >= 24 || (size >= 18.66 && weight >= 700);
    const floor = isLarge ? 3.0 : 4.5;
    const r = ratio(lum(fg), lum(bg));

    out.push({
      sel: desc(el), text: txt, size, weight,
      ratio: Math.round(r * 100) / 100, floor,
      ok: r >= floor,
      margin: Math.round((r - floor) * 100) / 100,
      dynamic: /color-mix|color\(|light-dark/.test(isSvgText ? cs.fill : cs.color),
      isLarge,
    });
  }
  return out;
};

(async () => {
  const argv = process.argv.slice(2);
  const showAll = argv.includes('--all');
  const forceDark = argv.includes('--dark');
  const files = argv.filter((a) => !a.startsWith('--'));
  if (!files.length) {
    console.error('用法: node tools/contrast_runtime.js <页面路径> [更多页面…] [--dark] [--all]');
    process.exit(2);
  }

  const { chromium } = loadPlaywright();
  const exe = findBrowser();
  if (!exe) {
    console.error('✗ 找不到本机 Chrome/Edge。请安装其一，或改用 --executable-path。');
    process.exit(2);
  }
  const browser = await chromium.launch({ executablePath: exe });

  let totalBad = 0;
  for (const rel of files) {
    const abs = path.isAbsolute(rel) ? rel : path.join(ROOT, rel);
    if (!fs.existsSync(abs)) { console.error(`✗ 不存在：${rel}`); continue; }

    for (const dark of forceDark ? [true] : [false, true]) {
      const ctx = await browser.newContext({
        viewport: { width: 1440, height: 900 },
        colorScheme: dark ? 'dark' : 'light',
      });
      const page = await ctx.newPage();
      await page.goto(pathToFileURL(abs).href, { waitUntil: 'load' });
      // 解除入场动效，否则大量元素还是 opacity:0 状态
      await page.addStyleTag({ content: '.reveal{opacity:1!important;transform:none!important} *{animation-duration:0s!important;transition-duration:0s!important}' });
      await page.evaluate(async () => {
        for (let y = 0; y < document.body.scrollHeight; y += 700) {
          window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 25));
        }
        window.scrollTo(0, 0);
      });
      await page.waitForTimeout(450);

      const rows = await page.evaluate(COLLECT);
      const bad = rows.filter((r) => !r.ok);
      // 0.10 与静态门禁 tools/check_contrast.py 保持同一口径
      const fragile = rows.filter((r) => r.ok && r.margin < 0.10);
      const dyn = rows.filter((r) => r.dynamic);
      totalBad += bad.length;

      console.log('='.repeat(104));
      console.log(`${rel}　${dark ? '暗色' : '浅色'}　共量 ${rows.length} 个文字元素`);
      console.log('='.repeat(104));
      if (!bad.length) {
        console.log('  ✓ 全部达标');
      } else {
        console.log(`  ❌ 不达标 ${bad.length} 项：`);
        console.log('  ' + '元素'.padEnd(30) + '文字'.padEnd(26) + '字号'.padEnd(12) + '实测'.padEnd(9) + '下限');
        console.log('  ' + '-'.repeat(100));
        for (const r of bad.sort((a, b) => a.margin - b.margin)) {
          console.log('  ' + r.sel.slice(0, 29).padEnd(30)
            + r.text.slice(0, 24).padEnd(26)
            + `${r.size}/${r.weight}`.padEnd(12)
            + `${r.ratio}:1`.padEnd(9) + r.floor
            + (r.dynamic ? '   ← color-mix 动态色' : ''));
        }
      }
      if (fragile.length) {
        console.log(`\n  ⚠ 贴线（达标但余量 < 0.15）${fragile.length} 项：`
          + fragile.map((r) => `${r.sel}(${r.ratio})`).slice(0, 6).join('、'));
      }
      if (dyn.length) {
        console.log(`  ⚠ 其中 ${dyn.length} 项颜色来自 color-mix()/color() 动态色 —— 静态令牌门禁看不到，只能靠本工具`);
      }
      if (showAll) {
        console.log('\n  全部元素：');
        for (const r of rows) console.log(`    ${r.ok ? '✅' : '❌'} ${r.ratio}:1  ${r.sel}  「${r.text}」`);
      }
      console.log();
      await ctx.close();
    }
  }

  await browser.close();
  if (totalBad) {
    console.log(`❌ 运行时对比度检查未通过，合计 ${totalBad} 项不达标`);
    process.exit(1);
  }
  console.log('✅ 运行时对比度检查通过');
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
