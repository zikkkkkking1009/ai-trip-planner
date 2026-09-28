"""接口级集成测试：覆盖此前**零测试引用**的 14 个接口。

为什么单独一个文件：
这些接口共同点是「有副作用或依赖外部状态」（读落盘 plans / 写收藏 / 拉高德），
单元测试覆盖不到，又不能靠"CI 没有 Key"这个巧合来保证不真发请求。
所以这里统一做三件事：

1. **数据隔离**：`PLANS_DIR` / `FAV_FILE` 是模块级常量，monkeypatch 到 tmp_path，
   测试绝不碰真实用户数据（否则跑一次 pytest 就清空你的历史规划）。
2. **零外呼**：需要 Key 的接口（`review` / `edit`）显式把 `load_env_file` 与
   env 打空，断言 **503 而不是让它真去调 LLM**（本机有 Key 就会真花钱）。
   `/poi/detail` 未命中缓存时会走网络 → 把 `fetch` 打桩，断言 404 分支。
3. **只锁行为，不锁实现**：断言取 HTTP 语义与关键字段，不比对整包 JSON，
   免得以后加个字段就红一片。

⚠️ 两条踩过坑的事实（写死在这里，改动前先读）：
- `_load_task_payload` 找不到任务抛 **404**，不是 400。
- `/poi/detail` 抓回来的数据**必须过城市校验**才准写入缓存 —— 历史上
  出现过「四川博物院 存了西安的数据」，一旦写入就永久固化。
"""
import json

import pytest
from fastapi.testclient import TestClient

import main
from main import app

client = TestClient(app)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把落盘路径指到临时目录，返回 (plans_dir, fav_file)。"""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(main, "PLANS_DIR", plans)
    monkeypatch.setattr(main, "FAV_FILE", tmp_path / "favorites.json")
    return plans, tmp_path / "favorites.json"


def _write_plan(plans, task_id, result=None, deleted=False):
    (plans / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id, "created_at": "2026-09-24T10:00:00",
        "deleted": deleted,
        "result": result if result is not None else {"days": []},
        "params": {"city": "西安"},
    }, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- /demo/config

def test_demo_config_returns_tile_info():
    """底图配置：前端靠 gcj 决定要不要做坐标系转换，缺了地图会整体偏移。"""
    r = client.get("/demo/config")
    assert r.status_code == 200
    d = r.json()
    assert "{x}" in d["tile_url"] and "{y}" in d["tile_url"]
    assert isinstance(d["gcj"], bool), "gcj 必须是布尔，前端直接当 if 条件用"


# ---------------------------------------------------------------- /demo/spots

def test_demo_spots_known_city_has_geometry():
    """已知城市：每个景点都要有经纬度，否则地图打不了点。"""
    city = main.demo_cities()[0]
    r = client.get("/demo/spots", params={"city": city})
    assert r.status_code == 200
    spots = r.json()["spots"]
    assert spots, f"{city} 应该有演示景点"
    for s in spots[:5]:
        assert -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180, s.get("name")


def test_demo_spots_unknown_city_not_silently_swapped():
    """未知城市必须返回空 + 提示，**不允许静默换成别的城市的数据**。

    这是产品语义（用户选了成都就绝不能给他西安的行程），静默兜底会骗人。
    """
    r = client.get("/demo/spots", params={"city": "火星市"})
    assert r.status_code == 200
    d = r.json()
    assert d["spots"] == []
    assert d["available"] is False, "必须明说该城市不可用，不能返回空列表假装成功"
    assert d["supported_cities"], "要顺带告诉用户支持哪些城市"


# ---------------------------------------------------------------- /poi/*

def test_poi_reviews_unrecorded_returns_404():
    """未收录地点：明确 404，不要返回空评价假装成功。"""
    r = client.get("/poi/reviews",
                   params={"name": "一个不存在的景点XYZ", "city": "西安"})
    assert r.status_code == 404


def test_poi_detail_miss_and_empty_payload_returns_404(monkeypatch):
    """缓存未命中 → 抓取为空 → 404（不是返回一堆空字符串让前端渲染空卡）。"""
    import fetch_spot_details
    monkeypatch.setattr(fetch_spot_details, "fetch", lambda n, c: {})
    r = client.get("/poi/detail",
                   params={"name": "一个不存在的景点XYZ", "city": "西安"})
    assert r.status_code == 404


def test_poi_detail_rejects_wrong_city_without_caching(monkeypatch):
    """**历史 bug 回归**：抓回来的数据属于别的城市时，必须 404 且**绝不写缓存**。

    为什么这条最要紧：缓存一旦被写脏就永久固化，之后每次都返回错误城市的数据，
    而且现场看不出来。所以这里额外用 spy 断言 `save_media` 根本没被调用。
    """
    import fetch_spot_details
    monkeypatch.setattr(
        fetch_spot_details, "fetch",
        lambda n, c: {"image": "http://x/a.jpg", "address": "成都市武侯区某某路"})
    monkeypatch.setattr(main, "suspected_wrong_city", lambda city, m: "成都")

    called = []
    monkeypatch.setattr(main, "update_media", lambda city, name, m: called.append(1))

    r = client.get("/poi/detail", params={"name": "某博物院", "city": "西安"})
    assert r.status_code == 404, "城市不符必须拒绝"
    assert called == [], "绝不能把别的城市的数据写进缓存"


# ---------------------------------------------------------------- /favorites

def test_favorites_empty_by_default(isolated):
    r = client.get("/favorites")
    assert r.status_code == 200
    assert r.json()["favorites"] == []


def test_favorites_corrupt_file_returns_empty(isolated):
    """收藏文件损坏时按空处理并留痕，不得 500（否则「我的」页整体不可用）。"""
    _, fav = isolated
    fav.write_text("{ 这不是合法 JSON", encoding="utf-8")
    r = client.get("/favorites")
    assert r.status_code == 200
    assert r.json()["favorites"] == []
    # 损坏文件保留到下一次写操作才被重建 —— 先 GET 不该偷偷清掉用户数据
    assert fav.read_text(encoding="utf-8").startswith("{")


def test_favorites_add_then_remove_roundtrip(isolated):
    """加 → 能查到；删 → 查不到。收藏是幂等操作，重复 add 不该产生两条。"""
    spot = {"name": "兵马俑", "city": "西安"}
    r = client.post("/favorites", json={"action": "add", "spot": spot})
    assert r.status_code == 200
    assert [f["name"] for f in r.json()["favorites"]] == ["兵马俑"]

    # 重复 add 不产生重复项
    r = client.post("/favorites", json={"action": "add", "spot": spot})
    assert len(r.json()["favorites"]) == 1, "重复收藏不应产生两条"

    r = client.post("/favorites", json={"action": "remove", "spot": spot})
    assert r.status_code == 200
    assert r.json()["favorites"] == []
    assert client.get("/favorites").json()["favorites"] == [], "删除要真的落盘"


def test_favorites_requires_name_and_valid_action(isolated):
    """缺 spot.name → 400；action 不是 add/remove → 400。

    为什么不静默忽略：这两种都是前端传错，静默吞掉会让"点了收藏没反应"
    变成一个极难排查的 bug。
    """
    assert client.post("/favorites", json={"action": "add", "spot": {}}).status_code == 400
    assert client.post(
        "/favorites", json={"action": "toggle",
                            "spot": {"name": "兵马俑"}}).status_code == 400


# ---------------------------------------------------------------- /plans

def test_list_plans_hides_soft_deleted(isolated):
    """软删除的规划不能出现在列表里 —— 否则用户会看到"删了还在"。

    顺带锁住：列表按 mtime 倒序（新的在前）。
    """
    plans, _ = isolated
    _write_plan(plans, "keep-1")
    _write_plan(plans, "gone-1", deleted=True)
    r = client.get("/plans")
    assert r.status_code == 200
    ids = [p["task_id"] for p in r.json()["plans"]]
    assert "keep-1" in ids and "gone-1" not in ids


def test_delete_plan_soft_delete_keeps_file(isolated):
    """删除是**软删除**：文件还在（可恢复），只是被标记。

    id 必须用合法格式（uuid4.hex[:12]）：task_id 现在有格式校验（防路径穿越），
    非法格式的测试 id 会被 404 挡在门外，测不到软删除本身。
    """
    plans, _ = isolated
    _write_plan(plans, "aaaaaaaaaaaa")
    r = client.delete("/plans/aaaaaaaaaaaa")
    assert r.status_code == 200 and r.json() == {"deleted": "aaaaaaaaaaaa"}
    assert (plans / "aaaaaaaaaaaa.json").exists(), "软删除不该物理删文件"
    assert json.loads((plans / "aaaaaaaaaaaa.json").read_text(encoding="utf-8"))["deleted"] is True
    # 再删一次同一个 id：文件还在，但列表里已看不到 → 幂等，不报错
    assert client.delete("/plans/aaaaaaaaaaaa").status_code == 200


def test_delete_plan_missing_returns_404(isolated):
    assert client.delete("/plans/does-not-exist").status_code == 404


def test_delete_plans_batch(isolated):
    plans, _ = isolated
    _write_plan(plans, "aaaaaaaaaaaa")
    _write_plan(plans, "bbbbbbbbbbbb")
    # cccccccccccc 格式合法但不存在 —— 测的是「存在性」，不是格式校验（另有穿越测试）
    r = client.post("/plans/delete", json={"task_ids": ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]})
    assert r.status_code == 200
    d = r.json()
    assert d["deleted"] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"], "不存在的 id 不该混进成功列表"
    assert d["count"] == 2


def test_delete_plans_batch_requires_ids(isolated):
    """空 task_ids → 400。否则"全选但一个没勾"会被当成清空所有。"""
    assert client.post("/plans/delete", json={"task_ids": []}).status_code == 400


# ---------------------------------------------------------------- /extract 输入约束

def test_extract_rejects_empty_and_oversized_text():
    """空文本 → 400；超长 → 413。两者都必须在碰 LLM 之前挡下。

    断言不触发 503：说明长度校验先于 Key 检查——CI 无 Key 也能跑，
    无鉴权接口的防滥用上限也不依赖「有没有配 Key」。
    """
    r = client.post("/extract", json={"text": "   "})
    assert r.status_code == 400
    r = client.post("/extract", json={"text": "x" * (main.EXTRACT_MAX_CHARS + 1)})
    assert r.status_code == 413
    assert str(main.EXTRACT_MAX_CHARS) in r.json()["detail"]


def test_plan_edit_rejects_oversized_instruction():
    """/plan/edit 同样要有输入上限（无鉴权接口防 token 滥用）。

    超长 → 400，先于 Key 检查与任务存在性检查（CI 无 Key 照样能测）。
    """
    r = client.post("/plan/edit", json={
        "task_id": "aaaaaaaaaaaa",
        "instruction": "x" * (main.EDIT_INSTRUCTION_MAX_CHARS + 1)})
    assert r.status_code == 400


# ---------------------------------------------------------------- /task

def test_get_task_disk_fallback(isolated):
    """内存里没有的任务，要从磁盘快照兜底返回 completed。

    为什么必须这样：服务重启后前端还在轮询旧 task_id，兜底不住就一片 404。
    """
    plans, _ = isolated
    _write_plan(plans, "aaaaaaaaaaa9", result={"days": [{"day": 1, "items": []}]})
    r = client.get("/task/aaaaaaaaaaa9")
    assert r.status_code == 200
    d = r.json()
    assert d["task_id"] == "aaaaaaaaaaa9" and d["status"] == "completed"
    assert d["result"]["days"], "兜底要把 result 带回去"


def test_get_task_unknown_returns_404(isolated):
    assert client.get("/task/nope").status_code == 404


def test_task_id_rejects_traversal(isolated):
    """task_id 来自无鉴权入参且直接拼路径，必须校验格式，否则能穿越出 PLANS_DIR。

    历史 bug：`POST /plans/delete` 传 `../x` 可对 PLANS_DIR 之外的任意 .json
    写 deleted 标记；`GET /task/{id}` 可读其 result 字段（Windows 下路径参数
    里的 %5C 反斜杠同样能穿越）。task_id 实际由 uuid4.hex[:12] 生成，
    收紧成 12 位 hex 零成本。
    """
    plans, _ = isolated
    outside = plans.parent / "outside.json"
    outside.write_text(json.dumps({"secret": 1}, ensure_ascii=False), encoding="utf-8")

    # 批量删除：穿越 id 不得碰到 PLANS_DIR 之外的文件（正斜杠 / 反斜杠两种写法）
    r = client.post("/plans/delete", json={"task_ids": ["../outside"]})
    assert r.status_code == 200 and r.json()["deleted"] == []
    r = client.post("/plans/delete", json={"task_ids": ["..\\..\\outside"]})
    assert r.json()["deleted"] == []
    assert json.loads(outside.read_text(encoding="utf-8")) == {"secret": 1}, \
        "穿越 id 改写了 PLANS_DIR 之外的文件"

    # 单删 / 轮询同样拒绝非法格式（%5C 反斜杠在 Windows 路径里是分隔符）
    assert client.delete("/plans/..%5C..%5Coutside").status_code == 404
    assert client.get("/task/..%5C..%5Coutside").status_code == 404
    assert json.loads(outside.read_text(encoding="utf-8")) == {"secret": 1}


def test_list_plans_surfaces_cost_known(isolated):
    """历史列表必须透出 cost_known：票价未知的行程不能被渲染成精确总价。

    契约回归：快照 result 里有 cost_known，但列表接口此前漏传，
    前端 p.cost_known 恒 undefined → moneyTxt 把「票价待查」显示成 ¥N。
    """
    plans, _ = isolated
    _write_plan(plans, "aaaaaaaaaaaa", result={"days": [], "cost_known": False})
    r = client.get("/plans")
    assert r.status_code == 200
    row = next(p for p in r.json()["plans"] if p["task_id"] == "aaaaaaaaaaaa")
    assert row["cost_known"] is False


# ---------------------------------------------------------------- /plan/* 子接口

def test_plan_simulate_unknown_task_returns_404(isolated):
    """⚠️ 是 404 不是 400：`_load_task_payload` 找不到任务就抛 404。"""
    assert client.post("/plan/simulate", json={"task_id": "nope"}).status_code == 404


def test_plan_review_without_key_returns_503(isolated, monkeypatch):
    """没配 LLM Key 时明确 503，让前端能说"未配置"，而不是转半天圈。

    Key 检查在取任务**之前**，所以这里不需要真实任务。
    必须把 env 打空：本机一旦配了 Key，不打空就会真去调 LLM（真花钱）。
    """
    import commute
    monkeypatch.setattr(commute, "load_env_file", lambda: {})
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = client.post("/plan/review", json={"task_id": "whatever"})
    assert r.status_code == 503


def test_plan_edit_requires_task_and_instruction(isolated):
    """缺字段 → 400（这个校验在 Key 检查之前，所以有 Key 也照样 400）。"""
    assert client.post("/plan/edit", json={"instruction": "加个咖啡馆"}).status_code == 400
    assert client.post("/plan/edit", json={"task_id": "t1"}).status_code == 400
    assert client.post("/plan/edit", json={"task_id": "t1", "instruction": "   "}).status_code == 400


def test_plan_edit_without_key_returns_503(isolated, monkeypatch):
    import commute
    monkeypatch.setattr(commute, "load_env_file", lambda: {})
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = client.post("/plan/edit", json={"task_id": "t1", "instruction": "加个咖啡馆"})
    assert r.status_code == 503


# ---------------------------------------------------------------- WebSocket

def test_ws_unknown_task_sends_not_found(isolated):
    """WS 连上未知任务：发一条 not_found 后关闭。

    为什么必须显式发：前端靠这条区分"还没开始"和"任务没了"，
    直接静默断开会让进度条永远停在 0%。
    """
    with client.websocket_connect("/ws/nope") as ws:
        msg = ws.receive_json()
    assert msg.get("status") == "not_found"
