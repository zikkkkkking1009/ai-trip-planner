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
3. 报告 `innerHTML` 插值中直接使用未转义外部字段的可疑位置（提示级）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "static" / "index.html"

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


def main() -> int:
    if not HTML.exists():
        print(f"找不到 {HTML}")
        return 1
    html = HTML.read_text(encoding="utf-8")
    js = extract_scripts(html)
    print(f"检查 {HTML.relative_to(ROOT)}（脚本 {len(js.splitlines())} 行）")
    errors = check_syntax(js) + check_shadowing(js) + check_hardcoded_coords(html)
    warns = check_unescaped(js)
    if warns:
        print(f"  ⚠ 未转义的外部字段 {len(warns)} 处（提示，不阻塞）：")
        for w in warns[:8]:
            print(f"     - {w}")
    if errors:
        print("\n❌ 检查未通过：")
        for e in errors:
            print(f"   - {e}")
        return 1
    print("\n✅ 前端静态检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
