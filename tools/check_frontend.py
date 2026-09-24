"""前端静态检查（CI 用）：语法门 + 全局函数遮蔽防护。

为什么需要这个：
2026-09-18 出现过一个"静默渲染中断"事故——`static/index.html` 里写了
`const esc = name.replace(...)`，把全局的 HTML 转义函数 `esc()` 遮蔽了。
由于 `const` 有暂时性死区，同一代码块内**所有** `esc(...)` 调用都会抛错，
异常又被 `.catch()` 吞掉：用户看到的现象是"景点详情少了很多内容"，
而控制台/后端日志都没有明显报错。定位成本很高，因此固化成检查项。

检查内容：
1. `<script>` 提取后交给 `node --check` 做语法门（缺 node 时跳过并提示）
2. 禁止在函数内声明与全局工具函数重名的局部变量（遮蔽）
3. 禁止 `src="${...}"` 与 `data-*="${...}"` 属性里插未转义的外部值（错误级）
4. 禁止 CSS 自定义属性引用自己（`--ok: var(--ok)`，浏览器静默丢弃）
5. 禁止 CSS 引用未定义的变量（`var(--x)` 而 `--x` 不存在，整条声明静默失效）
6. 禁止文档被复制/截断（`<html>`/`<body>` 单例 + 结构标签开闭配平）
7. 报告 `innerHTML` 插值中直接使用未转义外部字段的可疑位置（提示级）
8. 报告可见正文里残留的 Markdown 加粗标记（提示级）
9. 对比度门禁：委托 tools/check_contrast.py，按 WCAG 阈值量两页的配色令牌（错误级）
10. 禁止承载成段文字的容器内联写死颜色（错误级，覆盖两页 + docs/design 下两份设计稿）
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 需要检查的页面：首页与规划器都已从单页里拆开，两个都要进 CI 门禁
# （新增页面若漏进这份清单，就等于绕过了语法门与转义/坐标守卫）
PAGES = ("static/index.html", "static/home.html")

# 设计稿：它们用**同一套令牌**渲染（"规范即样张"），所以配色规则也得守；
# 但没有页面级 JS，不需要跑语法门/转义门，只跑第 10 项容器配色检查。
DOCS = ("docs/design/ui-design-system.html", "docs/design/home-redesign.html")

# 全局函数名：这些名字不允许被局部变量遮蔽
GLOBAL_FN_NAMES = {
    "esc", "fmt", "navTo", "navToSpot", "openLightbox", "openSpotDetail",
    "dateOf", "dayCount", "iso", "render", "renderItin", "renderMap",
    "renderPlans", "switchView", "switchPage", "loadPlans", "loadFavorites",
    "log", "toggleFav", "openHotelPicker", "pickHotel", "showLoading",
}

# 这些字段来自 LLM / 高德 / 用户输入，插入 innerHTML 前必须经过 esc()
EXTERNAL_FIELDS = ("v.name", "v.desc", "v.intro", "base.intro", "base.desc",
                   "d.intro", "d.address", "d.opentime", "f.name", "h.name",
                   "plan.hotel.name")


def extract_scripts(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def check_syntax(js: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        print("  ⚠ 未找到 node，跳过语法门（CI 中已配置 node）")
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        path = f.name
    proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    if proc.returncode != 0:
        return [f"JS 语法错误：{proc.stderr.strip()[:400]}"]
    print("  ✓ JS 语法通过")
    return []


def check_shadowing(js: str) -> list[str]:
    """扫描函数体，禁止局部变量与全局工具函数重名。"""
    errors: list[str] = []
    # 粗略切分函数体：从 'function name(' 到下一个顶层 'function ' 之间
    blocks = re.split(r"\n(?=function\s+\w+\s*\()", js)
    for block in blocks:
        m = re.match(r"function\s+(\w+)", block)
        where = m.group(1) if m else "顶层"
        for decl in re.finditer(r"\b(?:const|let|var)\s+(\w+)\s*=", block):
            name = decl.group(1)
            if name not in GLOBAL_FN_NAMES or name == where:
                continue
            # 只有当该名字在同一函数里被「当函数调用」（name( ）时，遮蔽才是真隐患；
            # 否则只是重名，不影响（例如顶层用 const 定义箭头函数）。
            if re.search(rf"\b{name}\s*\(", block):
                errors.append(
                    f"函数 {where} 内声明了局部变量 `{name}`，且同处把它当函数调用——"
                    f"会遮蔽同名全局函数（曾导致详情卡渲染中断），请改用其它名字")
    if not errors:
        print("  ✓ 无全局函数遮蔽")
    return errors


# 坐标字面量：与后端 check_backend_health.py 的 HARDCODED_COORD_HINTS 同口径。
# 前端曾经在 renderMap 里写死 [34.26, 108.94]（西安），而上一行注释还写着
# "原先写死西安坐标，是多城市泛化漏掉的一处"——注释改了、代码没改，所以必须机器守住。
HARDCODED_COORD_RE = re.compile(r"\b(34\.2\d*|34\.3\d*|108\.9\d*|104\.0\d*|116\.4\d*)\b")

# CSS 变量自引用：`--x: var(--x)` / `--x: var(--x, fallback)`。见 check_css_self_ref。
CSS_SELF_REF_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*var\(\s*\1\s*[,)]")


def check_hardcoded_coords(html: str) -> list[str]:
    """禁止前端出现城市坐标字面量：地图初始视野等一律从 /cities 拿。"""
    errors: list[str] = []
    for i, line in enumerate(html.splitlines(), 1):
        code = line.split("//")[0]        # 去掉行注释
        m = HARDCODED_COORD_RE.search(code)
        if m:
            errors.append(
                f"第 {i} 行：坐标字面量 {m.group(1)} —— 请用 cityCenters / "
                f"/cities 返回的坐标，不要写死（多城市泛化的静默错误源）")
    if not errors:
        print("  ✓ 无硬编码坐标")
    return errors


def check_unescaped_attrs(js: str) -> list[str]:
    """错误级：HTML **属性**里的动态值必须走 esc()。

    为什么值得单独一条：文本插值早就统一加了 `esc()`，属性里却漏过——图片 URL 来自
    高德 photos / 媒体缓存，`data-*` 里的坐标与 task_id 来自接口返回，都属不可信输入。
    属性里只要有一个双引号就能越出引号、注入 `onerror=` 之类的事件处理器。

    规则（刻意收窄，避免噪音）：
    - `src="${...}"`：一律要求 esc()
    - `data-*="${...}"`：只对**含属性访问**（有 `.`，即外部取值）的表达式要求 esc()；
      纯内部表达式如 `data-day="${i+1}"`、`data-idx="${i}"` 是循环下标，跳过
    """
    errors: list[str] = []
    for m in re.finditer(r'(src|data-[a-z-]+)="\$\{([^}]*)\}"', js):
        attr, expr = m.group(1), m.group(2)
        if "esc(" in expr:
            continue
        if attr != "src" and "." not in expr:
            continue          # data-* 里的内部表达式（循环下标等），不涉及外部输入
        line = js[:m.start()].count("\n") + 1
        errors.append(
            f"第 {line} 行：`{attr}=\"${{{expr.strip()[:40]}}}\"` 未走 esc()——"
            f"外部值含双引号即可越出属性注入事件处理器，请改用 esc(...)")
    if not errors:
        print("  ✓ 属性插值（src / data-*）均已转义")
    return errors


def check_css_self_ref(html: str) -> list[str]:
    """错误级：CSS 自定义属性不能引用自己（`--ok: var(--ok)`）。

    为什么值得单独一条：2026-09-23 用脚本把色值批量收敛成令牌时，脚本是
    「先插入新 :root、再全局替换色值」，于是刚写好的语义色令牌自己被替换成了
    `--ok: var(--ok)`。CSS 变量自引用**不是语法错误**——浏览器只当它是无效值
    静默丢弃，于是红/绿/橙全部失效（退回继承色），而 JS 语法门、五维审计、
    页面本身都不会报任何错。与项目一贯警惕的「静默错误」是同一类。
    别名（`--acc: var(--accent)`）是合法的，规则只认完全同名，不会误报。
    """
    errors: list[str] = []
    style = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    for m in CSS_SELF_REF_RE.finditer(style):
        line = style[:m.start()].count("\n") + 1
        errors.append(
            f"CSS 第 {line} 行：`{m.group(1)}: var({m.group(1)})` 是自引用——"
            f"不是语法错误，浏览器当无效值静默丢弃，该令牌与所有引用它的颜色全部失效")
    if not errors:
        print("  ✓ 无 CSS 变量自引用")
    return errors


def check_undefined_css_vars(html: str) -> list[str]:
    """错误级：CSS 里 `var(--x)` 引用了没定义的 `--x`。

    为什么值得单独一条：这类错误**完全不报错**。浏览器把整条声明当无效值丢掉，
    属性回退成继承 / 初始值。2026-09-23 做两页风格统一时连踩两次：
      · 首页 `border-color:var(--accent-line)` —— 首页 :root 里没这个令牌，
        hover 边框直接回退成 currentColor（深色），看起来像「故意设计成黑的」
      · 首页 `background:var(--fill)` —— 同样没定义
    两次都是靠截图脚本读 computedStyle 才发现的，机器守住成本更低。
    """
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", css))
    missing: dict[str, list[int]] = {}
    for m in re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)", css):
        name = m.group(1)
        if name not in defined:
            missing.setdefault(name, []).append(css[:m.start()].count("\n") + 1)
    errors = []
    for name, lines in sorted(missing.items()):
        where = ", ".join(str(n) for n in lines[:4])
        errors.append(
            f"CSS 引用了未定义的变量 {name}（第 {where} 行）——"
            f"浏览器会整条声明失效并回退成继承值，且不报任何错，请先在 :root 定义")
    if not errors:
        print("  ✓ 无未定义的 CSS 变量引用")
    return errors


# 容器级硬编码色：只认**承载成段文字**的容器标签。
# 为什么不用「全部内联色」一刀切：设计稿里的色卡（`<div class="sw__chip" style="background:#539af2">`）
# 是在**展示**色值，写死是对的；压在图上的蒙层（rgba 遮罩）本来就该与主题无关。
# 这两类是 div，所以把规则收窄到块级容器标签，实测四个文件命中 0 处、零误报。
CONTAINER_TAGS = ("section", "article", "aside", "main", "header", "footer")
CONTAINER_STYLE_RE = re.compile(
    r"<(section|article|aside|main|header|footer)\b[^>]*?\bstyle=\"([^\"]*)\"", re.I)
# 只看填色/描边这类「会造出新参照面」的属性，不看 width/filter 之类
INLINE_LITERAL_COLOR_RE = re.compile(
    r"(?<![\w-])(?:color|background|background-color|border|border-\w+-color)"
    r"\s*:\s*[^;\"']*?(#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\))")


def check_container_hardcoded_colors(html: str) -> list[str]:
    """错误级：承载成段文字的容器不得内联写死颜色。

    为什么值得单独一条：2026-09-24 给规范文档加「落地状态」块时，容器上写了
    `style="background:#eef6ee;border:1px solid #cde8cf"`，字色却走主题令牌。
    文档默认暗色（`apply(saved || 'dark')`）→ 浅灰字压浅绿底，实测 **1.1:1**，整块隐形。
    更隐蔽的是第二层：块内表格表头用 `--text-3`，而这个令牌的反解参照面是
    `--bg / --surface / --fill`，**从来没有针对 `--ok-soft` 解过** → 浅色 4.21:1、
    暗色 4.29:1，两套主题都被这层染色底拉成不达标。

    也就是说：容器一旦写死底色，就等于凭空造出一个「调色板没解过的新参照面」，
    块内所有令牌的达标前提全部失效——而这种错**不会报任何错**，只是看起来"淡了"。
    守卫成本远低于事后逐令牌复算。

    **边界（有意留白）**：只覆盖「容器级底色/描边」，这是根因那一类；
    页面上还有两类合法的硬编码色**不在**本项范围内，别误以为它管全部内联色：
      · 色卡（设计稿里在展示色值，`<div class="sw__chip">` 写死是对的）
      · 压在图/视频上的蒙层与标记描边（本来就该与主题无关）
    确有正当理由时，在 style 里写 `/* theme-independent */` 显式豁免：
    浏览器会把该注释当无效内容忽略，不影响解析，但规则会放行。
    """
    errors: list[str] = []
    for m in CONTAINER_STYLE_RE.finditer(html):
        tag, style = m.group(1).lower(), m.group(2)
        if "theme-independent" in style:
            continue
        for c in INLINE_LITERAL_COLOR_RE.finditer(style):
            line = html[:m.start()].count("\n") + 1
            errors.append(
                f"第 {line} 行：<{tag}> 的内联样式里写死了颜色 `{c.group(0).strip()[:48]}` ——"
                f"容器写死底色会造出令牌没解过的参照面，块内文字在另一套主题下必然不达标，"
                f"请改用令牌（如 var(--ok-soft) / var(--ok-line)），"
                f"确与主题无关则加 `/* theme-independent */` 豁免")
    if not errors:
        print("  ✓ 无容器级硬编码颜色")
    return errors


def check_unescaped(js: str) -> list[str]:
    """提示级：innerHTML 模板里直接插外部字段的位置。"""
    warns: list[str] = []
    for m in re.finditer(r"\$\{([^}]+)\}", js):
        expr = m.group(1)
        for field in EXTERNAL_FIELDS:
            if field in expr and "esc(" not in expr:
                line = js[:m.start()].count("\n") + 1
                warns.append(f"第 {line} 行：`${{{expr.strip()[:40]}}}` 未走 esc()")
    return warns


# 结构性标签：这些标签的数量必须配平，且 <body>/<head>/<html> 只能有一个
PAIRED_TAGS = ("section", "figure", "details", "footer", "svg")

# 注释里的标签不算数：CSS 注释里会写「折叠用原生 <details>」，JS 注释里会写
# 「给 <html> 加 js-reveal」—— 曾经因此让守卫误报（第一版就是这么翻车的）。
COMMENT_RE = re.compile(r"<!--.*?-->|/\*.*?\*/", re.S)


def check_document_integrity(html: str) -> list[str]:
    """错误级：文档必须是**单份且标签配平**的。

    为什么值得单独一条：2026-09-23 做首页改版收尾时，一个批量重排脚本里
    `parts[-1]` 已经包含在最后一组里、却又被 append 了一次，于是「图 5 之后的
    整段文档」被原样复制了第二遍（11737 字符）。

    这类损坏**特别能藏**：
      · 浏览器照常渲染 —— 重复内容只是多出一截普通 DOM，不报错、不空白
      · JS 语法门照常通过 —— 两个 `<script>` 块各自都是合法代码
      · 只看 `<figure>` 开标签也正常（数量仍是 5）
    只有 `</figure>`(6)、`</section>`(14)、`</html>`(2) 这类**收尾**标记会露馅。
    所以按「开闭配平 + 文档级单例」两个维度守，且统计前先剥掉注释。
    """
    code = COMMENT_RE.sub("", html)
    errors: list[str] = []

    # 单例标签：用词边界，否则 `<head` 会匹配到 `<header`
    for tag in ("html", "body", "head"):
        n_open = len(re.findall(rf"<{tag}[\s>]", code))
        n_close = len(re.findall(rf"</{tag}[\s>]", code))
        if n_open != 1 or n_close != 1:
            errors.append(f"`<{tag}>` 应各出现 1 次，实际开 {n_open} / 闭 {n_close} "
                          f"—— 文档被复制或截断过")

    for tag in PAIRED_TAGS:
        o = len(re.findall(rf"<{tag}[\s>]", code))
        c = len(re.findall(rf"</{tag}[\s>]", code))
        if o != c:
            errors.append(f"`<{tag}>` 开闭不配平：{o} 个开标签 / {c} 个闭标签 "
                          f"—— 批量改结构时很可能复制或漏掉了整段内容")
    if not errors:
        print("  ✓ 文档结构完整（单份、标签配平）")
    return errors


def check_markdown_leak(html: str) -> list[str]:
    """提示级：可见正文里残留 Markdown 标记（`**加粗**`）会原样渲染成星号。

    为什么值得提：首页正文是先在 Markdown 里写的，转成 HTML 时很容易漏掉一两处。
    `**待复测**` 就曾这样挂在页面上 —— 浏览器不报错，视觉上只是多两个星号，
    靠截图才发现。规则只扫**标签之间的可见文字**，所以 JS 的幂运算符（`a ** 2`）
    与注释里的强调写法都不会误报。
    """
    visible = html
    for pat in (r"<script>.*?</script>", r"<style>.*?</style>", r"<!--.*?-->"):
        visible = re.sub(pat, "", visible, flags=re.S)
    # 只取标签之间的文本
    texts = re.findall(r">([^<>]+)<", visible)
    warns = []
    for t in texts:
        for m in re.finditer(r"\*\*([^*\n]{1,30})\*\*", t):
            warns.append(f"可见正文里残留 Markdown 加粗标记 `**{m.group(1)}**`，"
                         f"会原样显示成星号，请改成 <strong>…</strong>")
    return warns


# ---------------------------------------------------------------------------
# 第 9 项：对比度门禁
# 委托给 tools/check_contrast.py，避免把 oklch→sRGB→WCAG 的色彩数学写两遍。
# ---------------------------------------------------------------------------
_CONTRAST_MOD = None


def _load_contrast_module():
    global _CONTRAST_MOD
    if _CONTRAST_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "check_contrast", Path(__file__).resolve().parent / "check_contrast.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTRAST_MOD = module
    return _CONTRAST_MOD


def check_contrast_tokens(rel: str) -> list[str]:
    """错误级：页面配色令牌必须满足 WCAG 对比度下限。

    为什么值得单独一条：2026-09-23 修「线性图标太淡看不见」时，改的是描边粗细
    （1.6px → 1.8px），而根因是图标色只有 2.88:1。**颜色改坏不会触发任何报错** ——
    页面照常渲染、JS 语法门照常通过、截图肉眼也未必看得出「淡了 0.3」。
    只有拿 WCAG 阈值去量才能发现，所以固化成检查项。

    令牌的「角色 × 参照底」登记在 check_contrast.py 的 BASE_ROLES / PAGE_EXTRA，
    阈值会按实际用法自动收紧（某令牌一旦有了文字用法，就必须满足正文 4.5）。
    已知历史债列在 KNOWN_DEBT，逐条附修复方案；它们不阻塞，但会随修复被提示删除。
    """
    try:
        module = _load_contrast_module()
    except Exception as exc:                      # noqa: BLE001
        print(f"  ⚠ 对比度门禁加载失败，跳过：{exc}")
        return []
    errors, debt, _unreg, _report, fragile = module.audit_page(rel, strict=False)
    if errors:
        return errors
    tail = ""
    if debt:
        tail += f"，历史债 {len(debt)} 项待偿还"
    if fragile:
        tail += f"，待复核 {len(fragile)} 项（贴线 / color-mix 动态色）"
    print(f"  ✓ 对比度达标{tail}")
    return []


def main() -> int:
    all_errors: list[str] = []
    for rel in PAGES:
        path = ROOT / rel
        if not path.exists():
            all_errors.append(f"{rel}：文件不存在")
            continue
        html = path.read_text(encoding="utf-8")
        js = extract_scripts(html)
        print(f"检查 {rel}（脚本 {len(js.splitlines())} 行）")
        errs = (check_syntax(js) + check_shadowing(js)
                + check_hardcoded_coords(html) + check_unescaped_attrs(js)
                + check_css_self_ref(html) + check_undefined_css_vars(html)
                + check_document_integrity(html) + check_contrast_tokens(rel)
                + check_container_hardcoded_colors(html))
        warns = check_unescaped(js) + check_markdown_leak(html)
        if warns:
            print(f"  ⚠ 提示 {len(warns)} 处（不阻塞）：")
            for w in warns[:8]:
                print(f"     - {w}")
        all_errors += [f"{rel}: {e}" for e in errs]
        print()
    for rel in DOCS:
        path = ROOT / rel
        if not path.exists():
            all_errors.append(f"{rel}：文件不存在")
            continue
        print(f"检查 {rel}（设计稿，仅配色）")
        errs = check_container_hardcoded_colors(path.read_text(encoding="utf-8"))
        all_errors += [f"{rel}: {e}" for e in errs]
        print()
    if all_errors:
        print("❌ 检查未通过：")
        for e in all_errors:
            print(f"   - {e}")
        return 1
    print("✅ 前端静态检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
