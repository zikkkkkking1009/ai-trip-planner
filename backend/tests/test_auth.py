"""访问令牌与限流的测试（公开部署闸门）。

为什么这批测试重要：
全站此前是"演示定位 = 无鉴权"，一旦映射到公网，**收藏/历史是全局共享**的，
而且任何人拿到链接就能触发 LLM 与高德调用（等于把 Key 交给陌生人烧）。
所以这两道闸门必须有测试锁住 —— 尤其是"该罩的接口有没有罩住"，
漏一个就等于闸门形同虚设。

⚠️ 令牌是**模块级常量**（import 时读取），测试用 monkeypatch 改它来模拟两种部署状态。
"""
from collections import defaultdict, deque

import pytest
from fastapi.testclient import TestClient

import main
from main import app

TOKEN = "test-token-abcdefghijklmnop"
client = TestClient(app)


@pytest.fixture
def with_token(monkeypatch):
    """模拟"已配好 APP_TOKEN"的生产状态。"""
    monkeypatch.setattr(main, "APP_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def no_token(monkeypatch):
    """模拟"还没上锁"的本地开发状态。"""
    monkeypatch.setattr(main, "APP_TOKEN", "")


# ---------------------------------------------------------------- 令牌校验

def test_protected_endpoint_rejects_without_token(with_token):
    """无令牌 → 401。用 GET /plans 是因为它不花钱、不依赖外部 Key。"""
    assert client.get("/plans").status_code == 401


def test_protected_endpoint_accepts_header_token(with_token):
    r = client.get("/plans", headers={"X-App-Token": TOKEN})
    assert r.status_code == 200


def test_protected_endpoint_accepts_bearer_token(with_token):
    """Authorization: Bearer 也要能用（curl / 脚本调用更方便）。"""
    r = client.get("/plans", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_wrong_token_rejected(with_token):
    """令牌不对同样是 401 —— 不能因为"带了头"就放行。"""
    assert client.get("/plans", headers={"X-App-Token": "wrong"}).status_code == 401
    assert client.get(
        "/plans", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_no_token_configured_allows_all(no_token):
    """未配置令牌时放行：本地开发 / CI 不该被这道闸门拦住。"""
    assert client.get("/plans").status_code == 200


def test_health_open_even_with_token(with_token):
    """`/health` 必须始终免令牌 —— Docker healthcheck 与外部监控要用。"""
    assert client.get("/health").status_code == 200


def test_pages_and_static_open(with_token):
    """页面本身要能打开：否则朋友连"输入令牌"的界面都看不到。

    `/meta` 同页面一起留开（首页要渲染服务状态），且它只回状态不回 Key。
    """
    for path in ("/", "/app", "/meta", "/cities", "/demo/config"):
        assert client.get(path).status_code == 200, path


def test_all_write_endpoints_are_protected(with_token):
    """**闸门覆盖面**：遍历真实路由表，逐条断言该罩的都罩住了。

    这条比逐个写死的用例更难绕过 —— 将来新增写接口时，只要忘了加依赖，
    这里就会红。判据：除白名单外，凡是有副作用/花钱的路径都必须 401。
    """
    # 留开清单（与 main.py 的注释一致）：监控、页面、静态、演示数据、城市表、
    # 语料检索（/ask 零 Key 只读，数据与 /demo/spots 同源公开）、服务状态
    open_paths = {"/health", "/", "/app", "/meta", "/cities",
                  "/demo/spots", "/demo/config", "/ask", "/static",
                  # FastAPI 自带的接口文档：**刻意留开**，不是漏网。
                  # ① 首页 footer 有意链到 /docs（作品集要展示 Swagger UI）；
                  # ② 文档只暴露接口形状，不泄露任何 Key；
                  # ③ 文档里"Try it out"能点的那些花钱接口，背后照样要令牌 —— 烧不到配额。
                  # 真要收，应该做成"受令牌保护的自定义 /docs"（FastAPI 的 docs_url=None +
                  # 自建路由），而不是简单关掉（那会把首页那个链接变成死链）。
                  "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    # 需要的路径参数值：用格式合法但必定不存在的 id（12 位 hex），
    # 这样"不是 401"就一定是漏了鉴权，而不是因为 404 侥幸通过
    sample = {"task_id": "000000000000", "name": "x", "city": "西安"}
    leaks = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/") or path in open_paths:
            continue
        if path.startswith("/static") or path.startswith("/ws/"):
            continue                      # 静态挂载与 WS 另有机制（WS 见下面的 4401 用例）
        if not methods & {"GET", "POST", "DELETE", "PUT", "PATCH"}:
            continue
        url = path.format(**sample)
        method = sorted(methods & {"GET", "POST", "DELETE", "PUT", "PATCH"})[0]
        r = client.request(method, url, json={})
        # 401 = 被令牌挡住（正确）；404 = 路由不存在（无泄漏可言）。
        # 其余（尤其 200）都算闸门漏洞。用这个判据而不是"必须 401"，
        # 是为了将来改路由时这条测试不会因为 404 而假红。
        if r.status_code not in (401, 404):
            leaks.append(f"{method} {path} → {r.status_code}")
    assert not leaks, "以下写接口未受令牌保护（闸门漏洞）：\n  " + "\n  ".join(leaks)


# ---------------------------------------------------------------- WebSocket

def test_ws_rejects_without_token(with_token):
    """WS 无令牌 → 关闭码 4401（浏览器 WS 不能带 header，令牌走 ?token=）。"""
    with client.websocket_connect("/ws/000000000000") as ws:
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_rejects_wrong_token(with_token):
    with client.websocket_connect("/ws/000000000000?token=wrong") as ws:
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_accepts_token_and_reports_not_found(with_token):
    """带对令牌 → 正常进入业务逻辑（未知任务回 not_found，而不是被鉴权拦掉）。"""
    with client.websocket_connect(f"/ws/000000000000?token={TOKEN}") as ws:
        assert ws.receive_json().get("status") == "not_found"


def test_ws_no_token_configured_allows(no_token):
    with client.websocket_connect("/ws/000000000000") as ws:
        assert ws.receive_json().get("status") == "not_found"


# ---------------------------------------------------------------- 限流

def test_rate_limit_returns_429(monkeypatch):
    """超过阈值 → 429（挡住"拿到令牌的人手抖/脚本把 Key 烧光"）。"""
    monkeypatch.setattr(main, "_rate_hits", defaultdict(deque))
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 3)
    codes = [client.get("/weather?city=西安&start=2026-10-01&end=2026-10-03").status_code
             for _ in range(5)]
    assert codes.count(429) >= 1, f"限流没生效：{codes}"
    assert codes[0] != 429, "第 1 次就被限流了，阈值算错"


def test_costly_path_matching_is_exact_not_naive_prefix():
    """额度是**全场共享**的（隧道不透传 IP），所以"少算"和"算错"都要防。

    裸 `startswith('/plan')` 会把只读的历史列表 `/plans` 也算进额度 ——
    那是纯磁盘读、不花钱，白白吃掉大家的额度。
    """
    for p in ("/plan", "/plan/async", "/plan/edit", "/extract",
              "/poi/detail", "/poi/reviews", "/hotel/search",
              "/hotel/recommend", "/hotel/set", "/weather"):
        assert main.is_costly_path(p), f"{p} 应该计入额度（它会花钱）"
    for p in ("/plans", "/plans/delete", "/plans/abc", "/task/000000000000",
              "/meta", "/health", "/cities", "/favorites", "/demo/spots",
              "/static/app.js", "/app", "/ws/000000000000"):
        assert not main.is_costly_path(p), f"{p} 不该计入额度（免费或只读）"


def test_rate_limit_does_not_count_cheap_paths(monkeypatch):
    """静态/健康检查不占额度 —— 否则页面正常浏览会把用户自己限掉。"""
    monkeypatch.setattr(main, "_rate_hits", defaultdict(deque))
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 2)
    for _ in range(5):
        assert client.get("/health").status_code == 200
    assert not main._rate_hits, "`/health` 不该被计入限流"


# ---------------------------------------------------------------- 真实 IP

def test_client_ip_takes_last_xff_from_loopback(monkeypatch):
    """⚠️ 隧道场景的关键点：花生壳的请求都来自本机，真实 IP 在 X-Forwarded-For。

    不取 XFF 的话，所有朋友会被算成同一个 IP，30 次/分钟 被全场共享。

    **而取的是最后一个值**：标准反代是**追加**（`客户端自带的` + `, ` + `真实 peer`），
    第一个元素恰恰是攻击者塞进来的。取第一个 = 用一个伪造头就换一个限流桶 → 限流白做。
    """
    class _Req:
        class client:
            host = "127.0.0.1"
        # 前面两个是攻击者伪造的，最后一个是可信代理追加的真实来源
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.7"}
    assert main.client_ip(_Req) == "203.0.113.7", \
        "取到了伪造的第一个值 —— 伪造 XFF 就能绕过限流"


def test_client_ip_single_xff_still_works(monkeypatch):
    """覆盖式写入（整条就是真实 IP）的实现也要正常。"""
    class _Req:
        class client:
            host = "127.0.0.1"
        headers = {"x-forwarded-for": "203.0.113.9"}
    assert main.client_ip(_Req) == "203.0.113.9"


def test_client_ip_ignores_spoofed_xff_from_lan(monkeypatch):
    """但 XFF 可伪造：直连方不是回环时**不能**采信，否则局域网内可随意刷额度。"""
    class _Req:
        class client:
            host = "192.168.1.50"
        headers = {"x-forwarded-for": "203.0.113.7"}
    assert main.client_ip(_Req) == "192.168.1.50"
