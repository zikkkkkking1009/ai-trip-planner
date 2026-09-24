#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批 2/3/4 令牌取值的推导与验证（可复跑）。

为什么单独一个文件：
    `tools/check_contrast.py` 校验的是「页面里当前的值达不达标」，
    但它回答不了「这个值当初是怎么定出来的」。本文件补上那一半 ——
    每个非显然的取值都是**反解**出来的（给定角色、参照底、目标对比度，
    二分求 oklch 亮度），而不是手挑的。

    规则（2026-09-24 定）：
      · 目标 = 阈值 + 0.12 余量。不要把值解到刚好等于阈值 ——
        oklch 取整成 hex 后对比度会漂移，已因此被门禁抓过一次。
      · 浅色主题「最坏背景」取最亮的那个面；暗色主题相反，取最亮的那个面
        （暗色下底越亮，亮字的对比越低）。
      · 一个令牌可能同时出现在多个面上（surface / bg / fill），必须全部满足。

跑法：
    python docs/design/solve-tokens.py
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------- 颜色内核
def to_lin_srgb(L: float, C: float, H: float):
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def _gamma(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1.055 * (x ** (1 / 2.4)) - 0.055 if x > 0.0031308 else 12.92 * x


def hex_of(L: float, C: float, H: float) -> str:
    return '#' + ''.join(f'{round(min(max(_gamma(c), 0), 1) * 255):02x}'
                         for c in to_lin_srgb(L, C, H))


def lum(L: float, C: float, H: float) -> float:
    r, g, b = to_lin_srgb(L, C, H)
    return (0.2126 * min(max(r, 0), 1) + 0.7152 * min(max(g, 0), 1)
            + 0.0722 * min(max(b, 0), 1))


def lum_hex(h: str) -> float:
    h = h.lstrip('#')
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def cr(a: float, b: float) -> float:
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def solve(C: float, H: float, target: float, refs: list[float]) -> float:
    """二分求 oklch 亮度：使该色压在 refs 里**每一个**参照面上都 >= target。"""
    want_dark = sum(refs) / len(refs) > 0.18      # 底亮 -> 找深字；底暗 -> 找亮字
    lo, hi = 0.0, 1.0
    for _ in range(220):
        mid = (lo + hi) / 2
        if all(cr(lum(mid, C, H), r) >= target for r in refs):
            if want_dark:
                lo = mid
            else:
                hi = mid
        else:
            if want_dark:
                hi = mid
            else:
                lo = mid
    return (lo + hi) / 2


MARGIN = 0.12

# ---------------------------------------------------------------- 参照面
L_SURFACE = lum(1.0, 0, 0)                  # --surface  #ffffff
L_BG      = lum(0.99, 0.002, 240)
L_FILL    = lum(0.97, 0.004, 250)
L_FILL2   = lum(0.945, 0.006, 250)
L_ACC_SOFT = lum(0.96, 0.02, 255)
L_ONACC   = lum_hex('#f7fcff')
L_OK_SOFT = lum_hex('#eef6ee')
L_WARN_SOFT = lum_hex('#fff4e6')
L_NOTE_SOFT = lum_hex('#fff8e6')

D_SURFACE = lum_hex('#14171b')
D_BG      = lum_hex('#0c0f12')
D_FILL    = lum_hex('#1e2124')
D_FILL2   = lum_hex('#262b2f')
D_OK_SOFT = lum_hex('#0e2517')
D_WARN_SOFT = lum_hex('#2f190f')
D_NOTE_SOFT = lum_hex('#2d1a0a')
D_BAD_SOFT  = lum_hex('#301715')

LIGHT_BASES = [L_SURFACE, L_BG, L_FILL2]
DARK_BASES  = [D_SURFACE, D_FILL2]

# ================================================================ 明色主题
LIGHT_SPEC = [
    # (令牌, 角色, 目标, C, H, 参照面)
    #
    # --muted-3 目标定 4.80（而非 4.62）是运行时实测逼出来的：
    # 它主要落在 --fill 上（折叠区 summary、输入占位），而 4.62 那版在浏览器里
    # 量到 4.59 —— 达标但余量只剩 0.09，属于贴线。多要 0.18 才稳。
    ('--muted-3',      '标签 / 元信息 / 占位',   4.80, 0.016, 255, [L_SURFACE, L_BG, L_FILL]),
    ('--ok',           '成功（有 12px 文字用法）', 4.5 + MARGIN, 0.16, 155, [L_SURFACE, L_FILL, L_OK_SOFT]),
    ('--warn-ink',     '警告文字（11–13px）',     4.5 + MARGIN, 0.16, 45, [L_WARN_SOFT, L_SURFACE]),
    ('--note-ink',     '提示文字',              4.5 + MARGIN, 0.15, 62, [L_NOTE_SOFT, L_SURFACE]),
    ('--bad',          '错误（有文字用法）',        4.5 + MARGIN, 0.19, 27, [L_SURFACE, L_FILL]),
    ('--accent-solid', '实心主色底',            4.5 + MARGIN, 0.16, 255, [L_ONACC]),
    ('--accent-line',  'hover / 选中态线',      3.0 + MARGIN, 0.09, 255, [L_SURFACE]),
    ('--c-ctrl',       '图表对照（实心灰）',      3.9, 0.012, 250, [L_SURFACE]),
    ('--c-ref',        '图表次级参考（空心）',     3.15, 0.010, 250, [L_SURFACE]),
]

print('=' * 98)
print('【明色主题】反解结果（目标 = 阈值 + %.2f 余量）' % MARGIN)
print('=' * 98)
print(f"{'令牌':<17}{'角色':<24}{'目标':>6}  {'oklch':<26}{'hex':<10}{'最坏实测':>10}")
print('-' * 98)
LIGHT: dict[str, tuple[float, float, float, str]] = {}
for name, role, tgt, C, H, refs in LIGHT_SPEC:
    L = solve(C, H, tgt, refs)
    v = hex_of(L, C, H)
    LIGHT[name] = (L, C, H, v)
    worst = min(cr(lum(L, C, H), r) for r in refs)
    print(f"{name:<17}{role:<24}{tgt:>6.2f}  {f'oklch({L*100:.1f}% {C} {H})':<26}{v:<10}{worst:>9.2f}:1")

print()
print('派生约束（这些组合也必须成立，但由上面的值连带决定）：')
print(f"  --on-accent(#f7fcff) 压 --accent-solid({LIGHT['--accent-solid'][3]}) = "
      f"{cr(L_ONACC, lum(*LIGHT['--accent-solid'][:3])):.2f}:1")
print(f"  --accent-deep 压 --accent-soft = {cr(lum(0.52,0.19,255), L_ACC_SOFT):.2f}:1"
      f"   （现值 oklch(52% 0.19 255) 已达标，无需改）")
print(f"  --accent 保留作图形  压白 = {cr(lum(0.58,0.18,255), L_SURFACE):.2f}:1   （>=3 即可）")
print(f"  图表两灰相互可辨      = {cr(lum(*LIGHT['--c-ctrl'][:3]), lum(*LIGHT['--c-ref'][:3])):.2f}:1   （>=1.2 才看得出是两组）")

# ================================================================ 暗色主题
print()
print('=' * 98)
print('【暗色主题】整套令牌 —— 项目此前 0 处暗色支持，这里逐档反解')
print('=' * 98)
DARK = {
    '--bg': '#0c0f12', '--surface': '#14171b', '--fg': '#dee0e2',
    '--muted': None, '--muted-2': '#adb2b6', '--muted-3': None,
    '--border': '#292e34', '--fill': '#1e2124', '--fill-2': '#262b2f',
    '--accent': '#539af2', '--accent-deep': '#539af2', '--accent-solid': '#2c7bd7',
    '--accent-ink': '#060c13', '--on-accent': '#060c13',
    '--accent-soft': '#102034', '--accent-line': None,
    '--ok': '#4ed589', '--ok-ink': '#009f57', '--ok-soft': '#0e2517', '--ok-line': '#1c4430',
    '--warn': '#f78955', '--warn-ink': '#da672c', '--warn-soft': '#2f190f',
    '--bad': None, '--bad-ink': '#e85854', '--bad-soft': '#301715', '--bad-line': '#5b2724',
    '--stay': '#8a82e9', '--note-ink': '#cc7200', '--note-soft': '#2d1a0a', '--note-line': '#5a3f14',
    '--unknown-ink': None, '--c-ctrl': None, '--c-ref': None,
}
DARK['--muted']       = hex_of(solve(0.012, 250, 5.4, DARK_BASES), 0.012, 250)
DARK['--muted-3']     = hex_of(solve(0.014, 255, 4.62, DARK_BASES), 0.014, 255)
DARK['--accent-line'] = hex_of(solve(0.10, 255, 3.12, [D_SURFACE]), 0.10, 255)
DARK['--bad']         = hex_of(solve(0.19, 27, 4.72, DARK_BASES), 0.19, 27)
DARK['--unknown-ink'] = hex_of(solve(0.013, 250, 5.0, DARK_BASES), 0.013, 250)
DARK['--c-ctrl']      = hex_of(solve(0.012, 250, 3.9, [D_SURFACE]), 0.012, 250)
DARK['--c-ref']       = hex_of(solve(0.010, 250, 3.15, [D_SURFACE]), 0.010, 250)

DARK_ASSERT = [
    ('--fg', '页面主文字', DARK_BASES, 4.5), ('--muted', '说明性正文', DARK_BASES, 4.5),
    ('--muted-2', '次级正文', DARK_BASES, 4.5), ('--muted-3', '标签 / 元信息', DARK_BASES, 4.5),
    ('--accent', '主色图形', [D_SURFACE], 3.0),
    ('--accent-line', 'hover / 选中态线', [D_SURFACE], 3.0),
    ('--ok', '成功（有文字用法）', DARK_BASES, 4.5), ('--ok-ink', '成功文字', [D_OK_SOFT], 4.5),
    ('--warn', '警告（有文字用法）', DARK_BASES, 4.5), ('--warn-ink', '警告文字', [D_WARN_SOFT], 4.5),
    ('--bad', '错误（有文字用法）', DARK_BASES, 4.5), ('--bad-ink', '错误文字', [D_BAD_SOFT], 4.5),
    ('--note-ink', '提示文字', [D_NOTE_SOFT], 4.5), ('--stay', '住宿标记', [D_SURFACE], 4.5),
    ('--unknown-ink', '无结论中性文字', DARK_BASES, 4.5),
    ('--c-ctrl', '图表对照', [D_SURFACE], 3.0), ('--c-ref', '图表次级参考', [D_SURFACE], 3.0),
]
print(f"{'令牌':<17}{'角色':<24}{'值':<10}{'实测':>9}  判定")
print('-' * 98)
all_ok = True
for name, role, refs, floor in DARK_ASSERT:
    v = DARK[name]
    r = min(cr(lum_hex(v), x) for x in refs)
    ok = r >= floor
    all_ok &= ok
    print(f"{name:<17}{role:<24}{v:<10}{r:>8.2f}:1  {'OK' if ok else 'FAIL <<<'}")
print()
print(f"  --on-accent 压 --accent-solid = {cr(lum_hex(DARK['--on-accent']), lum_hex(DARK['--accent-solid'])):.2f}:1")
print(f"  图表两灰相互可辨              = {cr(lum_hex(DARK['--c-ctrl']), lum_hex(DARK['--c-ref'])):.2f}:1")
print()
print('暗色全部达标：', 'OK' if all_ok else 'FAIL')
print()
print('=' * 98)
print('落地对照表（明色：替换前 -> 替换后）')
print('=' * 98)
BEFORE = {
    '--muted-3': 'oklch(68% 0.016 255)', '--ok': '#12b76a', '--bad': '#e03131',
    '--note-ink': '#ad6800', '--accent-line': 'oklch(88% 0.05 255)',
    '--accent-solid': '（新增）', '--warn-ink': '（新增）',
    '--c-ctrl': '#9aa3ad（仅首页）', '--c-ref': '#c9d0d8（仅首页）',
}
for k, before in BEFORE.items():
    print(f"  {k:<16}{before:<28}->  {LIGHT[k][3]}")

# ================================================================ 交叉校验
# 脚本输出必须与页面里的**实际值**逐项一致 —— 否则「规范里的数字能复跑」就是空话。
# 上面每个令牌的推导参数，就是落地时用的参数；这一段负责把这件事钉死。
print()
print('=' * 98)
print('交叉校验：本脚本算出的值 vs 两页 :root 里的实际值')
print('=' * 98)


def page_tokens(rel: str) -> dict[str, str]:
    import pathlib
    import re as _re
    src = (pathlib.Path(__file__).resolve().parents[2] / rel).read_text(encoding='utf-8')
    i = src.index(':root')                       # 第一个 :root 就是明色主题
    seg = src[i: src.index('}', i)]
    return {m.group(1): m.group(2)
            for m in _re.finditer(r'--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6}|oklch\([^)]*\))', seg)}


def value_lum(v: str) -> float:
    if v.startswith('#'):
        return lum_hex(v)
    parts = v[v.index('(') + 1: v.index(')')].replace('%', ' ').split()
    L, C, H = (float(x) for x in parts[:3])
    return lum(L / 100, C, H)


problems = 0
for rel in ('static/index.html', 'static/home.html'):
    toks = page_tokens(rel)
    print(f"  {rel}")
    for name, (L, C, H, v) in LIGHT.items():
        got = toks.get(name.lstrip('-'))
        label = name if name.startswith('--') else '--' + name
        if got is None:
            print(f"    {label:<16} 页面缺少该令牌（可能该页本就不需要）")
            continue
        # 容差 2.5e-3：hex 取整本身就会让亮度漂移约 0.0014（某个通道 128.49 -> 128），
        # 对应对比度差 <=0.03。这是噪声，不是不一致；真正写错值会差一个量级。
        same = abs(value_lum(got) - lum(L, C, H)) < 2.5e-3
        if not same:
            problems += 1
        print(f"    {label:<16} 脚本 {v}   页面 {got:<26} {'一致' if same else '不一致 <<<'}")
print()
print(f"不一致 {problems} 项" + ("　（0 = 文档与实现同源）" if problems == 0 else ""))
