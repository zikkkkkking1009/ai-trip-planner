"""后端健壮性五维审计（静态检查，不联网、不需要密钥）。

**为什么有这个脚本**：项目的五条底线是「超时 / 重试 / 状态 / 日志 / 成本」。
靠人记是记不住的——本轮就发现 `extractor` 缺常量导致接口 500、
`/poi/detail` 漏传城市导致非西安城市详情卡 404、多处 `except: pass` 静默吞异常。
把这些规则写成机器检查，接进 CI，就不会再靠「记得检查」了。

检查内容：
1. **超时**：所有外部调用（urllib.urlopen / OpenAI 客户端）必须显式带 timeout
2. **重试**：调用外部网络的函数必须有 retry_call（或在白名单里说明为什么不需要）
3. **状态**：异步任务函数必须 try/except 并在失败时把任务置为 failed（不能静默死掉）
4. **日志**：业务模块必须有 logger；except 分支必须有日志（禁止静默吞异常）
5. **成本**：限频/缓存/超时常量必须在位；不得硬编码城市名与坐标（多城市泛化回归守卫）

用法：
    python tools/check_backend_health.py            # 全量检查
    python tools/check_backend_health.py --json     # 机器可读输出

退出码：0 = 通过（可能有警告）；1 = 存在 ❌ 级违例
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"

# 纯数据/常量模块不需要 logger（它们不执行 I/O 与业务分支）
LOG_EXEMPT_MODULES = {
    "models.py", "cities.py", "constraint_check.py", "demo_data.py",
    "logging_setup.py",  # 它本身就是日志配置，再取 logger 无意义
    "media_cache.py",    # 只有读写与降级，关键失败已 log.warning（保留在豁免表是防未来改动）
    "fetch_spot_details.py",  # 一次性脚本：用 print 输出进度
    "run_demo.py", "run_demo_amap.py", "evaluation.py", "bench_models.py",
    "eval_aligner.py", "eval_gap.py", "eval_cluster.py", "eval_preference.py",
    "solver_cpsat.py",
}
# 明确允许不重试的外部调用（函数名: 理由）
RETRY_EXEMPT_FUNCS = {
    "_call_llm_arbiter": "对齐仲裁是可选增强，失败即降级为转人工，重试会拖慢交互",
    "fetch": "详情抓取失败降级为 404，前端已有占位；重试的收益低于延迟成本",
    "city_center": "高德地理编码，失败返回 None 由调用方提示用户选城市",
    "amap_get": "一次性数据生成脚本",
    "_amap_driving_min": "由调用方 commute.minutes 包 retry_call",
    "once": "本身作为 retry_call 的入参",
    "_llm": "只是构造客户端，实际调用点在 parse_instruction/generate_reviews 里已包 retry_call",
    "fake": "bench/eval 脚本内的假客户端（离线压测用）",
    "_with_provider": "bench 脚本，重试会干扰延迟测量",
    "llm_baseline_plan": "离线评估脚本，失败即记为该场景失败",
}
# 允许静默的 except 场景（代码里需有对应注释说明）
SILENT_EXCEPT_OK = {
    "json.JSONDecodeError", "OSError", "ValueError", "KeyError", "TypeError",
}

# 明确的超时下限（秒）：防止写成 0/None 等于不超时
MIN_TIMEOUT_S = 1.0


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, dim: str, where: str, msg: str) -> None:
        self.errors.append(f"[{dim}] {where}: {msg}")

    def warn(self, dim: str, where: str, msg: str) -> None:
        self.warnings.append(f"[{dim}] {where}: {msg}")


def _call_name(node: ast.AST) -> str:
    """取调用名：urlopen → urlopen；urllib.request.urlopen → urlopen；obj.f() → f"""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    return ""


def _has_kw(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _func_has_call(func: ast.AST, names: set[str]) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _call_name(node) in names:
            return True
    return False


def _is_log_call(node: ast.AST) -> bool:
    """判断是否为 log.xxx(...) 形式。

    注意坑：`log.exception(...)` 的 func 是 Attribute(value=Name('log'))，
    若用 `_call_name()` 会得到 'exception'，据此判断会把有日志的 except 全误报。
    所以必须检查 Attribute 的 value 是不是名为 log 的 Name。
    """
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
            and f.value.id == "log")


# 「有意静默」的说明关键词：except 里不写日志时，必须用注释显式声明理由
SILENT_INTENT_KEYS = ("降级", "忽略", "不影响", "可选", "无需", "尽力", "允许",
                      "noqa", "fallback", "容错", "跳过", "不阻断", "正常", "预期")


def _has_silent_intent(src_lines: list[str], node: ast.ExceptHandler) -> bool:
    """except 块内（或紧邻上一行）是否有说明「为什么可以静默」的注释。"""
    start = max(0, node.lineno - 2)
    end = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 1)
    for line in src_lines[start:end]:
        if "#" in line and any(k in line for k in SILENT_INTENT_KEYS):
            return True
    return False


# ---------- 维度 1：超时 ----------
NETWORK_CALLS = {"urlopen", "OpenAI", "Client", "AsyncClient"}
CALLS_REQUIRING_TIMEOUT = {"urlopen", "OpenAI"}


def check_timeouts(tree: ast.AST, path: Path, f: Findings) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in CALLS_REQUIRING_TIMEOUT:
            continue
        kw = _has_kw(node, "timeout")
        where = f"{path.name}:{node.lineno}"
        if kw is None:
            f.error("超时", where, f"{name}() 未设置 timeout —— 外部调用必须有超时")
            continue
        # timeout=None 或 0 等于没有超时
        v = kw.value
        if isinstance(v, ast.Constant) and (v.value is None or v.value == 0):
            f.error("超时", where, f"{name}() 的 timeout={v.value!r} 等于不超时")
        elif isinstance(v, ast.Constant) and isinstance(v.value, (int, float)):
            if v.value < MIN_TIMEOUT_S:
                f.warn("超时", where, f"timeout={v.value}s 偏短（建议 >= {MIN_TIMEOUT_S}s）")


# ---------- 维度 2：重试 ----------
RETRY_HINT_NAMES = {"urlopen", "OpenAI"}


def check_retries(tree: ast.AST, path: Path, f: Findings) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_net = any(
            isinstance(n, ast.Call) and _call_name(n) in RETRY_HINT_NAMES
            for n in ast.walk(node)
        )
        if not calls_net:
            continue
        if _func_has_call(node, {"retry_call"}):
            continue
        if node.name in RETRY_EXEMPT_FUNCS:
            continue
        f.warn("重试", f"{path.name}:{node.lineno}",
               f"{node.name}() 内有外部调用但没有 retry_call "
               f"（加入 RETRY_EXEMPT_FUNCS 并说明理由，或补重试）")


# ---------- 维度 3：任务状态 ----------
def check_task_states(tree: ast.AST, path: Path, f: Findings) -> None:
    if path.name != "tasks.py":
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("run_"):
            continue
        handlers = [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]
        if not handlers:
            f.error("状态", f"{path.name}:{node.lineno}",
                    f"{node.name}() 没有 try/except —— 后台任务失败会静默死掉")
            continue
        sets_failed = False
        for h in handlers:
            for n in ast.walk(h):
                if (isinstance(n, ast.Constant) and n.value == "failed"):
                    sets_failed = True
        if not sets_failed:
            f.error("状态", f"{path.name}:{node.lineno}",
                    f"{node.name}() 的 except 分支没有把任务置为 failed")


# ---------- 维度 4：日志 ----------
# 数据类异常：解析失败 / 字段缺失 / 类型不符 / 依赖缺失 —— 属于可预期的容错
BENIGN_EXC_TYPES = {"JSONDecodeError", "OSError", "KeyError", "ValueError",
                    "TypeError", "AttributeError", "IndexError", "UnicodeDecodeError",
                    "ImportError", "ModuleNotFoundError"}


def _exc_type_names(node: ast.ExceptHandler) -> set[str]:
    """取 except 声明的异常类型名（支持 Name、Tuple、以及 json.JSONDecodeError 这类 Attribute）。"""
    names: set[str] = set()
    t = node.type
    if isinstance(t, ast.Name):
        names.add(t.id)
    elif isinstance(t, ast.Attribute):
        names.add(t.attr)
    elif isinstance(t, ast.Tuple):
        for e in t.elts:
            if isinstance(e, ast.Name):
                names.add(e.id)
            elif isinstance(e, ast.Attribute):
                names.add(e.attr)
    return names


def _is_benign_data_except(node: ast.ExceptHandler) -> bool:
    """捕获的是数据/环境类异常（而非宽泛 Exception）→ 视为可预期容错。

    真正危险的是 `except Exception` —— 它会把编码错误也一起吞掉
    （本轮就踩过 NameError 被吞成 HTTP 500 的例子）。
    """
    names = _exc_type_names(node)
    return bool(names) and names <= BENIGN_EXC_TYPES


def _re_raises(node: ast.ExceptHandler) -> bool:
    """except 块内有 raise（向上传播/转成 HTTPException）→ 错误没有被吞掉。"""
    return any(isinstance(n, ast.Raise) for n in ast.walk(node))


# 离线脚本：用 print 输出进度、不进生产链路 → 日志维度整体豁免
SCRIPT_MODULES = {
    "fetch_spot_details.py", "run_demo.py", "run_demo_amap.py",
    "evaluation.py", "bench_models.py", "eval_aligner.py", "eval_gap.py",
    "eval_cluster.py", "eval_preference.py", "build_demo_data.py",
}


def check_logging(tree: ast.AST, path: Path, src: str, f: Findings) -> None:
    if path.name in SCRIPT_MODULES:
        return  # 离线脚本用 print，不算生产链路的日志缺口
    has_logger = any(
        isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "log" for t in n.targets)
        and isinstance(n.value, ast.Call) and _call_name(n.value) == "getLogger"
        for n in ast.walk(tree)
    )
    if not has_logger and path.name not in LOG_EXEMPT_MODULES:
        f.warn("日志", path.name, "没有 `log = logging.getLogger(__name__)`")

    src_lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if any(_is_log_call(n) for n in ast.walk(node)):
            continue  # 有日志，合规
        if _is_benign_data_except(node):
            continue  # 数据/环境类容错，合规
        if _re_raises(node):
            continue  # 错误向上传播（或转 HTTPException），没有被吞掉
        if _has_silent_intent(src_lines, node):
            continue  # 已用注释显式声明「有意静默」，合规
        f.warn("日志", f"{path.name}:{node.lineno}",
               "宽泛 except 无日志且未声明理由 —— 要么 log.warning，"
               "要么写注释说明为什么可以静默（关键词：降级/忽略/不影响…）")


# ---------- 维度 5：成本与多城市回归 ----------
REQUIRED_CONSTS = {
    "commute.py": ["QPS_MIN_INTERVAL", "CACHE_TTL_SEC", "AMAP_TIMEOUT_S"],
    "aligner.py": ["POI_TIMEOUT_S", "POI_CACHE_TTL_SEC", "AUTO_THRESHOLD"],
    "extractor.py": ["LLM_TIMEOUT_S"],
    "editor.py": ["LLM_TIMEOUT_S"],
}
# 允许出现城市字面量的位置（默认值定义、注释、白名单）
CITY_LITERAL_ALLOW = {
    "cities.py", "demo_data.py", "models.py", "tasks.py", "main.py",
    "fetch_spot_details.py", "editor.py", "extractor.py",  # extractor 的提示词里有示例 JSON
    "run_demo.py", "run_demo_amap.py", "evaluation.py", "bench_models.py",
}
CITY_NAMES = ("西安", "成都", "北京", "杭州", "重庆")
HARDCODED_COORD_HINTS = ("34.2", "34.3", "108.9", "108.94", "34.26")


def check_cost(tree: ast.AST, path: Path, src: str, f: Findings) -> None:
    for const in REQUIRED_CONSTS.get(path.name, []):
        defined = any(
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == const for t in n.targets)
            for n in ast.walk(tree)
        )
        if not defined:
            f.error("成本", path.name, f"缺少常量 {const}（限频/缓存/超时保护）")

    if path.name in CITY_LITERAL_ALLOW:
        return
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("#")[0]
        for city in CITY_NAMES:
            if f'"{city}"' in code or f"'{city}'" in code:
                f.error("成本", f"{path.name}:{i}",
                        f"代码里硬编码城市 {city!r} —— 多城市泛化回归，改用 cities.py 的常量")
        for hint in HARDCODED_COORD_HINTS:
            if hint in code:
                f.error("成本", f"{path.name}:{i}",
                        f"硬编码坐标 {hint} —— 请用 cities.city_center()")


def audit() -> Findings:
    f = Findings()
    for path in sorted(BACKEND.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            f.error("语法", path.name, f"无法解析: {e}")
            continue
        check_timeouts(tree, path, f)
        check_retries(tree, path, f)
        check_task_states(tree, path, f)
        check_logging(tree, path, src, f)
        check_cost(tree, path, src, f)
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description="后端健壮性五维审计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    f = audit()
    if args.json:
        print(json.dumps({"errors": f.errors, "warnings": f.warnings},
                         ensure_ascii=False, indent=2))
    else:
        dims = ["超时", "重试", "状态", "日志", "成本"]
        print("后端健壮性五维审计")
        print("  检查维度：" + " / ".join(dims))
        print()
        for dim in dims:
            errs = [x for x in f.errors if f"[{dim}]" in x]
            warns = [x for x in f.warnings if f"[{dim}]" in x]
            mark = "❌" if errs else ("⚠️ " if warns else "✅")
            print(f"{mark} {dim}：{len(errs)} 个错误 / {len(warns)} 个警告")
        if f.errors:
            print("\n错误（必须修）：")
            for e in f.errors:
                print("  " + e)
        if f.warnings:
            print("\n警告（可接受或需说明）：")
            for w in f.warnings:
                print("  " + w)
        print(f"\n合计：{len(f.errors)} 错误 / {len(f.warnings)} 警告")
    return 1 if f.errors else 0


if __name__ == "__main__":
    sys.exit(main())
