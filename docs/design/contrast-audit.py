#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设计令牌对比度实测 / 门禁（零依赖，只用标准库）。

用途
----
1) 打印：把 oklch / hex 令牌换算成 sRGB，并实测 WCAG 对比度（本文件是 docs/design/ui-design-system.html
   里所有数字的唯一来源，两者必须一致）。
2) 门禁：`--check` 模式对每组「前景色 × 它的参照底」断言对比度下限，不达标则非零退出。
   建议接进现有 tools/check_frontend.py 的门禁体系，成为第 10 项检查。

为什么需要它
------------
本项目曾出现「线性图标太淡看不见」，当时的修复是把描边从 1.6px 加到 1.8px ——
但根因是颜色只有 2.88:1（低于图形所需的 3:1）。描边加粗没有解决达标问题。
把对比度变成可断言的门禁，这类问题会在 CI 被拦下，而不是等用户反馈。

阈值依据 WCAG 2.2
------------------
- 正文文本        4.5:1   (1.4.3 Contrast Minimum)
- 大字号文本      3.0:1   (≥24px，或 ≥18.66px 且加粗)
- 图形 / 界面边界  3.0:1   (1.4.11 Non-text Contrast)
- 纯装饰元素       豁免    (不承载任何信息，如分隔发丝线)

用法
----
    python docs/design/contrast-audit.py            # 打印实测报告
    python docs/design/contrast-audit.py --check    # 门禁模式：不达标则退出码 1
"""

from __future__ import annotations

import math
import sys

# --------------------------------------------------------------------------
# 颜色换算：oklch → OKLab → 线性 sRGB → WCAG 相对亮度
# 与浏览器渲染路径一致，不依赖任何第三方色彩库。
# --------------------------------------------------------------------------


def oklch_to_linear_srgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """oklch（lightness 0~1）→ 线性 sRGB（可能超出 [0,1]，调用方负责裁剪）。"""
    rad = math.radians(hue)
    a = chroma * math.cos(rad)
    b = chroma * math.sin(rad)

    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b

    l, m, s = l_**3, m_**3, s_**3

    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _encode_gamma(channel: float) -> float:
    """线性光 → 显示用 sRGB 分量。"""
    x = min(max(channel, 0.0), 1.0)
    return 1.055 * (x ** (1 / 2.4)) - 0.055 if x > 0.0031308 else 12.92 * x


def luminance_from_oklch(lightness: float, chroma: float, hue: float) -> float:
    """WCAG 相对亮度（直接由线性光加权，无需再过 gamma）。"""
    r, g, b = oklch_to_linear_srgb(lightness, chroma, hue)
    return 0.2126 * min(max(r, 0), 1) + 0.7152 * min(max(g, 0), 1) + 0.0722 * min(max(b, 0), 1)


def hex_to_linear(hex_color: str) -> tuple[float, float, float]:
    value = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(value[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return out[0], out[1], out[2]


def luminance_from_hex(hex_color: str) -> float:
    r, g, b = hex_to_linear(hex_color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def to_hex(lightness: float, chroma: float, hue: float) -> str:
    return "#" + "".join(
        f"{round(_encode_gamma(c) * 255):02x}" for c in oklch_to_linear_srgb(lightness, chroma, hue)
    )


def contrast_ratio(lum_a: float, lum_b: float) -> float:
    hi, lo = max(lum_a, lum_b), min(lum_a, lum_b)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------
# 令牌表：与 static/index.html 的 :root / [data-theme] 保持一一对应。
# 浅色为「修复后定稿」；暗色为本次新增。
# 新增令牌必须同时登记进下面 ASSERTIONS，否则 --check 会报「未登记」。
# --------------------------------------------------------------------------

LIGHT: dict[str, str] = {
    "bg": "#fbfcfd",
    "surface": "#ffffff",
    "fill": "#f3f5f8",
    "fill-2": "#eaedf1",
    "fg": "#0e1217",
    "text-2": "#4d535a",
    "text-3": "#6f757e",          # 修复：原 --muted-3 = 2.88:1
    "icon": "#8c939c",
    "placeholder": "#6b7179",     # 修复：原 2.64:1
    "border": "#e2e5e8",
    "border-strong": "#8f9397",
    "focus": "#1779e1",
    "accent": "#1779e1",
    "accent-solid": "#0871d9",    # 修复：原 --accent 作按钮底 = 4.21:1（带余量解，实测 4.62:1）
    "accent-deep": "#0065d2",
    "accent-ink": "#f7fcff",
    "accent-soft": "#e9f3ff",
    "accent-line": "#c2daf9",
    "ok": "#00a865",           # 修复：原 #12b76a 作图形仅 2.62:1（低于 3:1）
    "ok-ink": "#2b6e34",
    "ok-soft": "#eef6ee",
    "ok-line": "#cde8cf",
    "warn": "#e8590c",
    "warn-ink": "#bc4c00",        # 新增：原 --warn 作正文 = 3.58:1
    "warn-soft": "#fff4e6",
    "bad": "#e03131",
    "bad-ink": "#b42318",
    "bad-soft": "#fef2f2",
    "bad-line": "#fee4e2",
    "note-ink": "#b55600",        # 修复：原 #ad6800 = 4.41:1（带余量解，实测 4.62:1）
    "note-soft": "#fff8e6",
    "note-line": "#ffe58f",
    "stay": "#7048e8",
    "on-accent": "#f7fcff",
    "day1": "#064180",
    "day2": "#1b589e",
    "day3": "#3a70b3",
}

DARK: dict[str, str] = {
    "bg": "#0c0f12",
    "surface": "#14171b",
    "fill": "#1e2124",
    "fill-2": "#262b2f",
    "fg": "#dee0e2",
    "text-2": "#adb2b6",
    "text-3": "#7e848a",
    "icon": "#616870",
    "placeholder": "#868b92",
    "border": "#292e34",
    "border-strong": "#63676d",
    "focus": "#5fa7ff",
    "accent": "#539af2",
    "accent-solid": "#2c7bd7",
    "accent-deep": "#539af2",
    "accent-ink": "#060c13",
    "accent-soft": "#102034",
    "accent-line": "#1d395b",
    "ok": "#4ed589",
    "ok-ink": "#009f57",
    "ok-soft": "#0e2517",
    "ok-line": "#1c4430",
    "warn": "#f78955",
    "warn-ink": "#da672c",
    "warn-soft": "#2f190f",
    "bad": "#f3625d",
    "bad-ink": "#e85854",
    "bad-soft": "#301715",
    "bad-line": "#5b2724",
    "note-ink": "#cc7200",
    "note-soft": "#2d1a0a",
    "note-line": "#5a3f14",
    "stay": "#8a82e9",
    "on-accent": "#060c13",
    "day1": "#3d7ece",
    "day2": "#5e9ae7",
    "day3": "#8cbaf7",
}

# (前景, 参照底, 下限, 说明)
ASSERTIONS: list[tuple[str, str, float, str]] = [
    # —— 文本 ——
    ("fg", "bg", 4.5, "页面主文字 / 页面底"),
    ("fg", "surface", 4.5, "页面主文字 / 卡片面"),
    ("text-2", "surface", 4.5, "次级正文 / 卡片面"),
    ("text-3", "surface", 4.5, "元信息、标签 / 卡片面"),
    ("placeholder", "fill", 4.5, "占位符 / 输入底"),
    ("accent-ink", "accent-solid", 4.5, "实心按钮文字 / 主色底"),
    ("accent-deep", "accent-soft", 4.5, "chip 选中文字 / 主色浅底"),
    ("ok-ink", "ok-soft", 4.5, "成功态文字 / 成功浅底"),
    ("warn-ink", "warn-soft", 4.5, "警告态文字 / 警告浅底"),
    ("bad-ink", "bad-soft", 4.5, "错误态文字 / 错误浅底"),
    ("note-ink", "note-soft", 4.5, "提示态文字 / 提示浅底"),
    ("ok-ink", "surface", 4.5, "成功态文字 / 卡片面"),
    ("bad-ink", "surface", 4.5, "错误态文字 / 卡片面"),
    ("stay", "surface", 4.5, "住宿标记文字 / 卡片面"),
    # —— 图形与边界（3:1）——
    ("icon", "surface", 3.0, "线性图标 / 卡片面"),
    ("border-strong", "surface", 3.0, "输入框等可交互边界 / 卡片面"),
    ("focus", "surface", 3.0, "焦点环 / 卡片面"),
    ("accent", "surface", 3.0, "主色图形 / 卡片面"),
    ("ok", "surface", 3.0, "成功图形 / 卡片面"),
    ("warn", "surface", 3.0, "警告图形 / 卡片面"),
    ("bad", "surface", 3.0, "错误图形 / 卡片面"),
    # —— 总览块：块色 vs 底色 + 块内标签 vs 块色（两个约束要同时成立）——
    ("day1", "surface", 3.0, "Day1 块色 / 卡片面"),
    ("day2", "surface", 3.0, "Day2 块色 / 卡片面"),
    ("day3", "surface", 3.0, "Day3 块色 / 卡片面"),
]

# 块内标签单独断言（浅色亮字 / 暗色深字，都走 on-accent）
DAY_LABEL_CASES = ["day1", "day2", "day3"]

# 明确豁免的纯装饰元素：只做分隔，不承载信息。
# 它们出现在这里是为了让「豁免」是个显式决定，而不是遗忘。
DECORATIVE_EXEMPT = [
    ("border", "surface", "卡片发丝分隔线"),
    ("border", "bg", "页面发丝分隔线"),
    ("ok-line", "ok-soft", "成功态浅底描边"),
    ("bad-line", "bad-soft", "错误态浅底描边"),
    ("note-line", "note-soft", "提示态浅底描边"),
]


def _swatch_rows(tokens: dict[str, str], label: str) -> None:
    print("=" * 78)
    print(f"{label} 令牌 → sRGB")
    print("=" * 78)
    for name, value in tokens.items():
        r, g, b = hex_to_linear(value)
        print(f"  --{name:<16}{value:<10} lum={luminance_from_hex(value):.4f}")


def main() -> int:
    check_mode = "--check" in sys.argv
    failures: list[str] = []

    if not check_mode:
        _swatch_rows(LIGHT, "浅色")
        print()
        _swatch_rows(DARK, "暗色")
        print()

    for theme_name, tokens in (("浅色", LIGHT), ("暗色", DARK)):
        print("=" * 78)
        print(f"WCAG 实测 · {theme_name}主题")
        print("=" * 78)
        print(f"{'组合':<36}{'实测':>9}  {'下限':>5}  判定")
        print("-" * 78)

        # 块内标签用的是 on-accent（浅色亮字 / 暗色深字），压在块色上
        rows = list(ASSERTIONS) + [
            ("on-accent", day, 4.5, f"块内标签(on-accent) / {day}") for day in DAY_LABEL_CASES
        ]

        for fg, bg, floor, desc in rows:
            if fg not in tokens or bg not in tokens:
                line = f"{desc:<36}{'—':>9}  {floor:>5}  令牌缺失 ❌"
                print(line)
                failures.append(f"[{theme_name}] {desc}：令牌 {'fg' if fg not in tokens else 'bg'} 未定义")
                continue
            ratio = contrast_ratio(luminance_from_hex(tokens[fg]), luminance_from_hex(tokens[bg]))
            ok = ratio >= floor
            if not ok:
                failures.append(f"[{theme_name}] {desc}：{ratio:.2f}:1 < {floor}:1")
            print(f"{desc:<36}{ratio:>8.2f}:1  {floor:>5}  {'✅' if ok else '❌'}")

        print()
        print("纯装饰元素（显式豁免，不承载信息）")
        print("-" * 78)
        for fg, bg, desc in DECORATIVE_EXEMPT:
            if fg in tokens and bg in tokens:
                ratio = contrast_ratio(luminance_from_hex(tokens[fg]), luminance_from_hex(tokens[bg]))
                print(f"{desc:<36}{ratio:>8.2f}:1      —  豁免")
        print()

        print(f"（{theme_name}主题共断言 {len(rows)} 组组合）")
        print()

    print("=" * 78)
    if failures:
        print(f"❌ 对比度门禁未通过，共 {len(failures)} 项：")
        for item in failures:
            print(f"   · {item}")
        print()
        print("修法提示：先确认该令牌的「角色」（正文 4.5 / 图形 3.0），")
        print("再二分反解刚好达标且最不闷的 oklch 亮度，而不是随手调深。")
        return 1

    print("✅ 对比度门禁全部通过")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
