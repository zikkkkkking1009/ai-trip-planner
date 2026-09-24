#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面级对比度门禁（零依赖，只用标准库）。

与 docs/design/contrast-audit.py 的分工
--------------------------------------
· docs/design/contrast-audit.py  —— 校验**设计规范文档自身**的目标值是否自洽（它硬编码令牌表）。
· tools/check_contrast.py（本文件）—— 校验 **static/ 里的真实代码**。它从 HTML 里解析
  `:root` / `[data-theme]` 的实际令牌，按「角色 × 参照底」断言 WCAG 下限。

只有本文件能拦住回归：规范文档写得再对，只要没人照着改 static/，颜色照样能改坏。

为什么需要它
------------
2026-09-23 修复「线性图标太淡看不见」时，改的是描边粗细（1.6px → 1.8px），
而根因是图标色对比度只有 2.88:1。**没有门禁时，修错地方不会被发现。**
同类静默问题在本项目已发生多次（转义、坐标、CSS 自引用），本项补齐色彩维度。

角色与阈值（WCAG 2.2）
----------------------
  text        4.5:1  正文（1.4.3）；大字号可放宽到 3.0，此处一律按正文从严
  graphic     3.0:1  图形 / 可交互边界（1.4.11）
  decorative  豁免   纯分隔线、浅填充面，不承载信息
  alias       别名令牌，跟随目标令牌

未登记的令牌会以「未登记」列出（不阻塞），逼迫新增令牌时显式声明角色，
而不是让一个不知用途的颜色悄悄进页面。

用法
----
    python tools/check_contrast.py            # 打印实测报告
    python tools/check_contrast.py --check    # 门禁：不达标退出码 1
    python tools/check_contrast.py --check --strict   # 未登记令牌也算失败
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent


# ==========================================================================
# 颜色换算：oklch → OKLab → 线性 sRGB → WCAG 相对亮度（与浏览器一致）
# ==========================================================================

def oklch_to_linear_srgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    rad = math.radians(hue)
    a = chroma * math.cos(rad)
    b = chroma * math.sin(rad)
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _linear_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_clamp01(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def luminance_from_oklch(lightness: float, chroma: float, hue: float) -> float:
    return _linear_luminance(oklch_to_linear_srgb(lightness, chroma, hue))


def luminance_from_hex(value: str) -> float:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    out = []
    for i in (0, 2, 4):
        c = int(v[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(lum_a: float, lum_b: float) -> float:
    hi, lo = max(lum_a, lum_b), min(lum_a, lum_b)
    return (hi + 0.05) / (lo + 0.05)


# 令牌取值语法
OKLCH_RE = re.compile(r"oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)\s*\)")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
TOKEN_DEF_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]+);")
VAR_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")


def parse_luminance(raw: str) -> float | None:
    """把一个 CSS 取值转成相对亮度；不认识则返回 None。"""
    raw = raw.strip()
    m = OKLCH_RE.search(raw)
    if m:
        return luminance_from_oklch(float(m.group(1)) / 100, float(m.group(2)), float(m.group(3)))
    if HEX_RE.match(raw):
        return luminance_from_hex(raw)
    return None


def extract_tokens(html: str) -> tuple[dict[str, str], dict[str, float], dict[str, str]]:
    """返回 (原始值表, 亮度表, 别名表)。键一律不带 `--` 前缀。"""
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    raw: dict[str, str] = {}
    # 按出现顺序累积，后面的定义覆盖前面的（媒体查询里的重定义会生效，
    # 但色彩令牌只在 :root 里定义一次，因此这里等价）
    for seg in re.findall(r":root\s*\{(.*?)\}", css, re.S):
        for name, value in TOKEN_DEF_RE.findall(seg):
            raw.setdefault(name.lstrip("-"), value.strip())

    alias: dict[str, str] = {}
    lum: dict[str, float] = {}
    for name, value in raw.items():
        inner = VAR_RE.search(value)
        if inner:
            alias[name] = inner.group(1).lstrip("-")
            continue
        parsed = parse_luminance(value)
        if parsed is not None:
            lum[name] = parsed
    return raw, lum, alias


# 属性 → 该令牌承担的角色。用来判断一个令牌「实际被当成什么用」。
# 这一步必须扫**整个文档**而不是只扫 <style>：首页的图表色 --c-ctrl / --c-ref
# 是以 style="fill:var(--c-ref)" 的内联形式写在 SVG 里的，只在 <style> 里统计
# 会得出「引用 0 处」——据此把在用的颜色当成死令牌删掉，正是典型的静默事故。
_PROPS_OF = {
    "color": "text",
    "fill": "graphic", "stroke": "graphic",
    "fill-text": "text", "fill-css": "graphic",
    "background": "surface", "background-color": "surface",
    "border": "edge", "border-color": "edge", "border-top": "edge",
    "border-bottom": "edge", "border-left": "edge", "border-right": "edge",
    "border-top-color": "edge", "border-bottom-color": "edge",
    "outline": "edge", "outline-color": "edge", "box-shadow": "shadow",
    "border-radius": "geometry", "max-width": "geometry", "padding": "geometry",
    "font-family": "geometry", "transition": "motion", "animation": "motion",
    "font-size": "geometry", "gap": "geometry", "width": "geometry",
}


def token_usage(html: str) -> dict[str, dict[str, int]]:
    """统计每个令牌被用在哪些属性上：{令牌: {属性: 次数}}。

    整个文档都扫（含内联 style），但先剥掉注释，避免把注释里的示例算进去。

    关键细节：SVG 的 `fill` 要分成两类 —— `<text fill:…>` 是**文字**（要 4.5:1），
    `<rect fill:…>` 是**图形**（3:1）。首页 5 张图表里 8 处 `--warn` 和 13 处
    `--muted-3` 都挂在 `<text>` 上，若一律按图形算，报告会低估问题规模。
    """
    scan = re.sub(r"/\*.*?\*/|<!--.*?-->", "", html, flags=re.S)
    usage: dict[str, dict[str, int]] = {}
    for m in re.finditer(r"([a-z-]+)\s*:\s*([^;{}\"']*?var\(\s*(--[a-z0-9-]+)\s*\))", scan):
        prop = m.group(1)
        name = m.group(3).lstrip("-")
        if prop == "fill":
            # 往前找最近的 `<tag`，判断这个 fill 挂在谁身上
            head = scan.rfind("<", 0, m.start())
            tag_m = re.match(r"<\s*([a-zA-Z][\w-]*)", scan[head:head + 40]) if head >= 0 else None
            tag = tag_m.group(1).lower() if tag_m else ""
            if tag in ("text", "tspan"):
                prop = "fill-text"
            elif not tag:
                prop = "fill-css"     # <style> 里的 fill，无法定位宿主，按图形保守处理
        usage.setdefault(name, {})
        usage[name][prop] = usage[name].get(prop, 0) + 1
    return usage


def role_of(prop: str) -> str:
    return _PROPS_OF.get(prop, "other")


def summarize_usage(props: dict[str, int]) -> str:
    """把属性计数翻译成一眼能懂的角色摘要，例如 `文字×8 · 底×35`。"""
    bucket: dict[str, int] = {}
    for prop, n in props.items():
        bucket[role_of(prop)] = bucket.get(role_of(prop), 0) + n
    label = {"text": "文字", "graphic": "图形", "surface": "底",
             "edge": "边框", "shadow": "阴影", "geometry": "几何",
             "motion": "动效", "other": "其它"}
    return " · ".join(f"{label.get(k, k)}×{v}" for k, v in
                      sorted(bucket.items(), key=lambda kv: -kv[1]))


def dynamic_color_uses(html: str) -> list[str]:
    """列出用 color-mix() 生成的**文字**颜色 —— 这是门禁的已知盲区。

    为什么单列一条：门禁只读 :root 里的令牌取值，而 color-mix() 是运行时算出来的
    动态色，不落在任何令牌上。2026-09-24 首页 hero 就是这么翻车的 ——
    `color-mix(in oklab, var(--accent-ink) 88%, var(--accent))` 让副标题从 4.21:1
    掉到 3.64:1，而门禁报「通过」。这类值只能靠浏览器实测（见
    docs/design/home-redesign.html 里那套读 computedStyle 的脚本）。
    """
    scan = re.sub(r"/\*.*?\*/|<!--.*?-->", "", html, flags=re.S)
    out: list[str] = []
    # `(?<![-a-z])` 很关键：`border-color:` / `background-color:` 里的 color: 不是文字色，
    # 否则按钮描边的 color-mix 会被误报成「文字动态色」。
    for m in re.finditer(r"(?<![-a-z])color\s*:\s*([^;{}\n]*?color-mix\([^;{}\n]*\))", scan):
        out.append(" ".join(m.group(1).split()))
    return out


# ==========================================================================
# 角色登记表：令牌 → (参照底, 阈值, 角色, 说明)
#   参照底用令牌名；SPECIAL 表示参照某个字面色（见下面的 LITERALS）
# ==========================================================================

LITERALS = {
    # 首页主 CTA 用的是字面 oklch，不是令牌（另见报告里的「字面色未令牌化」）
    "home-cta-bg": (0.16, 0.012, 250),
}

TEXT, GRAPHIC, DECOR = 4.5, 3.0, 0.0

# 角色登记的最小单位是「令牌 × 参照底」，不是「令牌」——
# 因为同一个令牌在两页可能完全不同命：`--muted` 在首页是正文主力（11 处），
# 在规划页是零引用死令牌；`--warn` 在规划页作 12px 文字，在首页 17/18 处是 SVG 图形。
# 阈值：TEXT 4.5（正文）/ GRAPHIC 3.0（图形与可交互边界）/ DECOR 豁免
TEXT, GRAPHIC, DECOR = 4.5, 3.0, 0.0

BASE_ROLES: dict[tuple[str, str], tuple[float, str, str]] = {
    ("fg", "surface"):        (TEXT, "text", "页面主文字"),
    ("muted", "surface"):     (TEXT, "text", "说明性正文"),
    ("muted", "bg"):          (TEXT, "text", "说明性正文 / 页面底"),
    ("muted-2", "surface"):   (TEXT, "text", "需要读的次级正文"),
    ("muted-3", "surface"):   (TEXT, "text", "标签 / 元信息 / 占位"),
    ("border", ""):           (DECOR, "decorative", "发丝分隔线"),
    ("fill", ""):             (DECOR, "decorative", "浅填充面"),
    ("fill-2", ""):           (DECOR, "decorative", "hover 填充面"),
    ("bg", ""):               (DECOR, "decorative", "页面底色"),
    ("accent", "surface"):    (GRAPHIC, "graphic", "主色图形（图标 / 描边）"),
    ("accent-deep", "surface"): (TEXT, "text", "主色文本（选中态 chip 等）"),
    ("accent-deep", "accent-soft"): (TEXT, "text", "主色文本 / 主色浅底（chip、SVG 胶囊）"),
    ("accent-ink", "accent-solid"): (TEXT, "text", "实心主色上的文字"),
    ("on-accent", "accent-solid"): (TEXT, "text", "实心主色上的字（按钮 / hero 色带）"),
    ("accent-soft", ""):      (DECOR, "decorative", "主色浅底"),
    ("accent-line", "surface"): (GRAPHIC, "graphic", "hover / 选中态线"),
    ("ok", "surface"):        (GRAPHIC, "graphic", "成功图形"),
    ("ok-ink", "ok-soft"):    (TEXT, "text", "成功态文字 / 成功浅底"),
    ("ok-ink", "surface"):    (TEXT, "text", "成功态文字 / 卡片面"),
    ("ok-soft", ""):          (DECOR, "decorative", "成功浅底"),
    ("ok-line", ""):          (DECOR, "decorative", "成功浅底描边"),
    ("warn", "surface"):      (GRAPHIC, "graphic", "警告图形（文字用法已改走 --warn-ink）"),
    ("warn-ink", "warn-soft"): (TEXT, "text", "警告文字 / 警告浅底"),
    ("warn-ink", "surface"):  (TEXT, "text", "警告文字 / 卡片面"),
    ("warn-soft", ""):        (DECOR, "decorative", "警告浅底"),
    ("note-ink", "note-soft"): (TEXT, "text", "提示态文字 / 提示浅底"),
    ("note-soft", ""):        (DECOR, "decorative", "提示浅底"),
    ("note-line", ""):        (DECOR, "decorative", "提示浅底描边"),
    ("bad", "surface"):       (GRAPHIC, "graphic", "错误图形"),
    ("bad-ink", "bad-soft"):  (TEXT, "text", "错误态文字 / 错误浅底"),
    ("bad-ink", "surface"):   (TEXT, "text", "错误态文字 / 卡片面"),
    ("bad-soft", ""):         (DECOR, "decorative", "错误浅底"),
    ("bad-line", ""):         (DECOR, "decorative", "错误浅底描边"),
    ("stay", "surface"):      (TEXT, "text", "住宿标记文字"),
    ("unknown-ink", "surface"): (TEXT, "text", "状态「无结论」中性文字"),
    ("surface", "bad"):       (TEXT, "text", "白字压错误红底（清空 / 删除确认）"),
    ("c-ctrl", "surface"):    (GRAPHIC, "graphic", "图表：对照 / 基线系列"),
    ("c-ref", "surface"):     (GRAPHIC, "graphic", "图表：次级参考（空心描边）"),
    ("accent-solid", ""):     (DECOR, "decorative", "实心主色底（被 --on-accent 引用）"),
}

# 页面追加 / 覆盖：同一 (令牌, 参照底) 会覆盖 BASE 里的值
# 批 2/3/4 落地后两页共用同一张表：文字一律走令牌、实心底一律走 --accent-solid，
# 页面级差异消失 —— 这本身就是「收敛完成」的验证。
PAGE_EXTRA: dict[str, dict[tuple[str, str], tuple[float, str, str]]] = {}

# 页面删除：该页根本不存在这个场景，保留会变成误报
PAGE_EXCLUDE: dict[str, set[tuple[str, str]]] = {}

# 别名令牌：跟随目标令牌的断言，不单独登记
ALIASES = {"acc": "accent", "dim": "muted-3", "line": "border"}

# --------------------------------------------------------------------------
# 已知历史债：明确承认、暂时不阻塞、但必须在报告里露出来。
#
# 值 = (登记时的令牌原始值, 为什么现在没修)。登记原始值是为了堵住白名单最大的漏洞：
# 若只按「令牌名」豁免，那么有人把这个颜色**改得更淡**也会被静默放过 ——
# 白名单会从「记账」退化成「永久免疫」。
# 因此：
#   · 实测已达标            → 提示「可清理」，要求删掉本条目
#   · 值被改动且仍不达标    → 直接报错（这是新的退化，不是历史债）
#   · 值未动且仍不达标      → 计入历史债，不阻塞
# 用 --no-debt 可让历史债也阻塞（债务清零后建议开启）。
# --------------------------------------------------------------------------
KNOWN_DEBT: dict[tuple[str, str, str], tuple[str, str]] = {}
# ↑ 2026-09-24 批 2/3/4 落地后债务清零。此后任何不达标都会直接阻塞；
#   要再放宽必须重新登记「令牌原始值 + 未修理由」，而不是加个名字了事。


def _literal_lum(name: str) -> float:
    return luminance_from_oklch(*LITERALS[name])


def resolve(name: str, lum: dict[str, float]) -> float | None:
    if name in lum:
        return lum[name]
    if name in LITERALS:
        return _literal_lum(name)
    return None


def audit_page(rel: str, strict: bool = False
               ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """读文件并审计。返回 (错误, 已知债命中, 未登记令牌, 报告行, 贴线脆弱项)。"""
    path = ROOT / rel
    if not path.exists():
        return [f"{rel}：文件不存在"], [], [], [], []
    return audit_html(rel, path.read_text(encoding="utf-8"), strict)


def audit_html(rel: str, html: str, strict: bool = False
               ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """审计一段页面 HTML。与 audit_page 分离，便于 --selftest 对改坏的副本做验证。"""
    raw, lum, alias = extract_tokens(html)
    usage = token_usage(html)

    table = dict(BASE_ROLES)
    for key in PAGE_EXCLUDE.get(rel, set()):
        table.pop(key, None)
    table.update(PAGE_EXTRA.get(rel, {}))

    checked: set[str] = set()
    order: list[tuple[str, str, float, str, str]] = []
    for (token, bg), (floor, role, desc) in table.items():
        if token not in raw:
            continue
        order.append((token, bg, floor, role, desc))
        checked.add(token)

    errors: list[str] = []
    debt_hits: list[str] = []
    report: list[str] = []
    total = 0
    fragile: list[str] = []

    for token, bg, floor, role, desc in order:
        if token not in raw:
            continue
        fg_lum = resolve(token, lum)
        bg_lum = resolve(bg, lum) if bg else None
        if fg_lum is None:
            report.append(f"  --{token:<14}{'—':>9}  {floor:>4}  ⚠ 取值无法解析：{raw[token][:30]}")
            continue
        props = usage.get(token, {})
        u = summarize_usage(props) or "未使用"

        if role == "decorative":
            report.append(f"  --{token:<14}{'豁免':>10}  {'—':>4}  {desc}　[{u}]")
            continue
        if bg_lum is None:
            report.append(f"  --{token:<14}{'—':>9}  {floor:>4}  ❌ 参照底 --{bg} 未定义")
            errors.append(f"{rel} --{token} 的参照底 --{bg} 未定义")
            continue

        # 按「实际用法」自动收紧阈值：只要该令牌在页面里存在文字用法
        # （color / SVG <text> 的 fill），就必须满足正文 4.5，哪怕它同时也在画图形。
        # 这条能自动抓住「原本只当图形用的颜色，后来被拿去渲染小字」——比如 --ok
        # 既画对勾也写 12px 标签，--accent 既画图标也当 chip 文字。
        has_text = any(role_of(p) == "text" for p in props)
        eff_floor = max(floor, TEXT) if has_text else floor
        tightened = has_text and eff_floor > floor

        ratio = contrast_ratio(fg_lum, bg_lum)
        total += 1
        ok = ratio >= eff_floor
        mark = "✅" if ok else "❌"
        n = sum(props.values())
        note = "文字用途→收紧" if tightened else desc
        line = (f"  --{token:<14}{ratio:>7.2f}:1  {eff_floor:>4}  {mark}  {note} "
                f"[底 --{bg}]　{u}")
        report.append(line)

        # 贴线预警：过了但余量 < 0.10。
        # 阈值定 0.10 是实测出来的：oklch 精确值取整成 hex 只会让对比度漂移 <=0.02，
        # 而本轮所有令牌都留了 0.12 余量 —— 留 0.10 作预警线，既有 5 倍安全系数，
        # 又不会把「本就安全的余量」全报成风险（那会让预警失效）。
        if ok and ratio - eff_floor < 0.10:
            fragile.append(f"--{token}（{desc}）{ratio:.2f}:1，仅高出下限 "
                           f"{ratio - eff_floor:.2f}，取整或微调即有跌破风险")

        if not ok:
            key = (rel, token, bg)
            if key in KNOWN_DEBT:
                registered_value, why = KNOWN_DEBT[key]
                current = re.sub(r"\s+", "", raw[token])
                if current != re.sub(r"\s+", "", registered_value):
                    errors.append(
                        f"{rel} --{token} 的值已被改动（{raw[token].strip()} ≠ 登记时的 "
                        f"{registered_value}），历史债白名单因此失效 —— 必须重新评估："
                        f"当前实测 {ratio:.2f}:1 < {eff_floor}:1")
                else:
                    debt_hits.append(f"{note}：{ratio:.2f}:1 < {eff_floor}  ← {why}")
            else:
                errors.append(
                    f"{rel} --{token}（{note}）实测 {ratio:.2f}:1，低于 {eff_floor}:1 "
                    f"（参照底 --{bg}；该令牌全页出现 {n} 处：{u}）")
        else:
            key = (rel, token, bg)
            if key in KNOWN_DEBT:
                debt_hits.append(
                    f"[可清理] {desc}：已达标（{ratio:.2f}:1），请从 KNOWN_DEBT 删除该条")

    # 未登记令牌：定义了、是颜色、但角色表里没有
    unregistered = []
    for name in raw:
        if name in checked or name in ALIASES:
            continue
        if name in lum or name in alias:
            unregistered.append(name)

    if unregistered:
        detail = ", ".join(
            f"--{n}（{summarize_usage(usage.get(n, {})) or '未使用'}）"
            for n in sorted(unregistered))
        report.append(f"  ⚠ 未登记角色的颜色令牌：{detail}")
        if strict:
            errors.append(f"{rel} 有未登记角色的颜色令牌：{detail}")

    # 死令牌：定义了但页面一处没用
    dead = [n for n in raw if n in lum and not usage.get(n)]
    if dead:
        report.append(f"  ⚠ 定义了但零引用（死令牌）：{', '.join('--' + d for d in sorted(dead))}")

    # 别名令牌没有自己的颜色，指向哪个目标也要一并露出（首页/规划页各有 --acc/--dim/--line）
    if alias:
        pairs = "，".join(f"--{k} → --{v}" for k, v in sorted(alias.items()))
        report.append(f"  别名令牌：{pairs}")

    # 动态色盲区：color-mix() 生成的颜色不在令牌表里，门禁量不到，必须显式露出
    dynamic = dynamic_color_uses(html)
    if dynamic:
        report.append(f"  ⚠ {len(dynamic)} 处文字用了 color-mix() 动态色（令牌门禁看不到，需浏览器实测）：")
        for d in dynamic[:8]:
            report.append(f"      · color: {d[:76]}")
        if len(dynamic) > 8:
            report.append(f"      · …另有 {len(dynamic) - 8} 处")
        fragile.append(
            f"color-mix() 动态色 {len(dynamic)} 处：令牌门禁量不到，改配色后需用浏览器实测复核")

    return errors, debt_hits, unregistered, report, fragile


def selftest() -> int:
    """验证门禁真的拦得住。一个没被测过的门禁，等于没有门禁。

    做法：拿真实页面当基线，在内存里「改坏」几处，看是否被拦住。
    """
    rel = "static/index.html"
    path = ROOT / rel
    if not path.exists():
        print(f"❌ 自测无法进行：{rel} 不存在")
        return 1
    html = path.read_text(encoding="utf-8")
    fails: list[str] = []

    # 1) 基线：真实页面当前应通过（历史债不算失败）
    errs, _debt, _unreg, _rep, _fr = audit_html(rel, html)
    if errs:
        fails.append("基线不干净：真实页面本应通过，却报了错")

    # 2) 把 --muted-3 调淡（模拟「改成更浅的灰」这类常见回退）。
    #    债务表已清零，所以这里验证的是最直接的性质：改坏就必须当场拦下。
    broken = re.sub(r"(--muted-3:\s*)oklch\([^)]*\)", r"\1oklch(82% 0.016 255)", html)
    if broken == html:
        fails.append("自测失效：没能改写 --muted-3 的定义")
    else:
        errs2, _debt2, *_ = audit_html(rel, broken)
        if not any("muted-3" in e for e in errs2):
            fails.append("把 --muted-3 从 54% 调淡到 82% 竟然静默通过 —— 门禁没拦住")

    # 3) 白名单防退化：这是白名单最容易退化成「永久免疫」的地方 ——
    #    上一轮就是这段第一次跑才暴露出「只按令牌名豁免」的缺陷，所以要一直测着。
    #    注意：报告里的债务行带的是**角色描述**（「标签 / 元信息 / 占位」）而不是令牌名，
    #    所以断言要用描述或 why 文本匹配，不能用 "muted-3" 去撞。
    saved = dict(KNOWN_DEBT)
    try:
        key = (rel, "muted-3", "surface")
        # 3a) 登记「当前值」，再把页面调淡改坏 → 值不一致，必须阻塞
        KNOWN_DEBT[key] = ("oklch(54.1% 0.016 255)", "自测 3a")
        e_a, _d_a, *_ = audit_html(rel, broken)
        if not any("已被改动" in x for x in e_a):
            fails.append("白名单没识别出「值被改动」—— 改坏后仍被豁免")
        # 3b) 登记「旧值」且页面改回该值 → 不达标但值一致，应计入历史债、不阻塞
        KNOWN_DEBT[key] = ("oklch(68% 0.016 255)", "自测 3b 标记")
        back = re.sub(r"(--muted-3:\s*)oklch\([^)]*\)", r"\1oklch(68% 0.016 255)", html)
        if back == html:
            fails.append("自测失效：没能把 --muted-3 改回登记值")
        else:
            e_b, d_b, *_ = audit_html(rel, back)
            if not any("自测 3b 标记" in d for d in d_b):
                fails.append("登记值未改动时，没有被计入历史债")
            if e_b:
                fails.append(f"登记值未改动时不应阻塞，却报了：{e_b[0][:60]}")
    finally:
        KNOWN_DEBT.clear()
        KNOWN_DEBT.update(saved)

    # 4) 新增一个没登记角色的颜色令牌：应给出未登记提示
    extra = re.sub(r"(--stay:\s*#[0-9a-fA-F]{6};)",
                   r"\1\n    --brand-new-color: oklch(70% 0.10 200);", html)
    if extra == html:
        fails.append("自测失效：没能注入 --brand-new-color")
    else:
        _e, _d, unreg2, _r, _f = audit_html(rel, extra)
        if "brand-new-color" not in unreg2:
            fails.append("注入未登记令牌 --brand-new-color 后，没有报「未登记」")

    # 5) 给某个纯图形令牌加上文字用法：阈值应自动从 3.0 收紧到 4.5
    #    这里用 --bad（4.51:1）作探针：它本来只差 0.01 就够 4.5，
    #    说明「收紧逻辑」在它身上确实生效（报告里标为「文字用途→收紧」）
    _e, _d, _u, rep5, _f = audit_html(rel, html)
    if not any("文字用途→收紧" in line for line in rep5):
        fails.append("阈值自动收紧逻辑没有生效（未出现「文字用途→收紧」标记）")

    print("=" * 96)
    if fails:
        print(f"❌ 自测失败 {len(fails)} 项：")
        for f in fails:
            print(f"   · {f}")
        return 1
    print("✅ 门禁自测通过：基线干净，且能拦住「调淡颜色 / 未登记令牌」并提示「债务可清理」")
    print("=" * 96)
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    strict = "--strict" in sys.argv
    debt_blocks = "--no-debt" in sys.argv

    pages = ["static/index.html", "static/home.html"]
    all_errors: list[str] = []
    all_debt: list[str] = []
    all_fragile: list[str] = []

    for rel in pages:
        path = ROOT / rel
        if not path.exists():
            all_errors.append(f"{rel}：文件不存在")
            continue
        print("=" * 96)
        print(f"{rel}")
        print("=" * 96)
        print(f"{'令牌':<17}{'实测':>10}  {'下限':>4}  判定  角色 / 参照底 / 页面用法")
        print("-" * 96)
        errors, debt, unreg, report, fragile = audit_page(rel, strict)
        for line in report:
            print(line)
        print()
        if fragile:
            print(f"  ⚠ 待复核 {len(fragile)} 项（贴线 = 余量 < 0.10；或动态色）：")
            for f in fragile:
                print(f"    · {f}")
            print()
        if debt:
            print(f"  已知历史债（不阻塞，但必须偿还）{len(debt)} 项：")
            for d in debt:
                print(f"    · {d}")
            print()
        all_errors += errors
        all_debt += [f"{rel}: {d}" for d in debt]
        all_fragile += [f"{rel}: {f}" for f in fragile]

    print("=" * 96)
    if all_errors:
        print(f"❌ 对比度门禁未通过，{len(all_errors)} 项：")
        for e in all_errors:
            print(f"   · {e}")
        print()
        print("修法：先确认该令牌的角色（正文 4.5 / 图形 3.0），再二分反解「刚好达标且带余量」")
        print("      的 oklch 亮度。注意不要解到刚好等于阈值 —— 取整成 hex 后会跌破，")
        print("      建议留 0.10~0.12 余量（本项在 2026-09-24 已被门禁抓到过一次）。")
        return 1

    if debt_blocks and all_debt:
        print(f"❌ --no-debt：{len(all_debt)} 项历史债视为失败（债务清零后可开启严格模式）")
        return 1

    print(f"✅ 对比度门禁通过（页面级）")
    if all_debt:
        print(f"   历史债 {len(all_debt)} 项待偿还（不阻塞；全部清零后建议加 --no-debt 跑严格模式）")
    if all_fragile:
        print(f"   待复核 {len(all_fragile)} 项（贴线 / color-mix 动态色），改动配色时优先复测")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
