/**
 * 落地页死链门禁：查所有 `href="#xxx"` 是否真有对应的 `id="xxx"`。
 *
 * 为什么要这道门（2026-10-10 真实踩到）：
 *   重做落地页时删掉了 `#features` 与 `#results` 两个 section，
 *   但导航栏和 hero 次要按钮还指向它们 ⇒ **用户点「量化结果」直接跳不动**。
 *   而 check_frontend 的 9 项全过（它查 JS 语法/转义/对比度/标签配平），
 *   check_contrast 也过 —— **没有一项会查锚点是否存在**。
 *
 * 属���：「删 section / 改 id」之后必须跑，否则等于发一个点了没反应的按钮。
 * CI 里已接（见 .github/workflows/ci.yml）。
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const PAGES = ['static/index.html', 'static/home.html', 'static/support.html'];

let total = 0, bad = 0;
for (const rel of PAGES) {
  const f = path.join(REPO, rel);
  if (!fs.existsSync(f)) { console.log('跳过（不存在）', rel); continue; }
  const html = fs.readFileSync(f, 'utf-8');

  // 收集所有 id
  const ids = new Set();
  for (const m of html.matchAll(/\bid="([^"]+)"/g)) ids.add(m[1]);

  // 收集所有页内锚点
  const anchors = [];
  for (const m of html.matchAll(/href="#([^"]+)"/g)) anchors.push(m[1]);

  const dead = [...new Set(anchors.filter(a => a && !ids.has(a)))];
  total += anchors.length;
  if (dead.length) {
    bad += dead.length;
    console.log(`  ✗ ${rel}: ${dead.length} 个死锚点 → ${dead.join(' ')}`);
  } else {
    console.log(`  ✓ ${rel}: ${anchors.length} 个页内锚点全部有对应 id`);
  }
}
console.log(`\n共检查 ${total} 个页内锚点，${bad} 个死链`);
process.exit(bad ? 1 : 0);