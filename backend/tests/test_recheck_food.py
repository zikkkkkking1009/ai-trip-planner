"""结果侧编辑（/plan/recheck）、美食情报卡（/food）、景点搜索（/poi/search）。

三条 2026-09-29 新接口的行为锁定：
- recheck：信任边界（只认名字，数值全重算）与剔除语义（装不下→unplanned 不 500）
- food：白名单城市文件名、同名去重更新、原子写
- poi/search：401 闸门 + 打桩后的结果形状
"""
import json

import pytest
from fastapi.testclient import TestClient

import main
from main import app
from models import Spot

TOKEN = "test-token-abcdefghijklmnop"
client = TestClient(app)


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setattr(main, "APP_TOKEN", TOKEN)
    return TOKEN


def _no_amap(monkeypatch, tmp_path):
    """本机 .env 有真 Key：不掐掉，recheck 里的 CommuteMatrix 会真发请求。"""
    import commute as commute_mod
    monkeypatch.setattr(commute_mod, "load_env_file", lambda: {})
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.setattr(commute_mod, "CACHE_FILE", tmp_path / "cm.json")


def _make_task(days=1, with_dates=True):
    """构造一个内存中的 completed 任务（带 request_spots，recheck 的成员集合）。"""
    from tasks import MANAGER
    spots = [
        Spot(source_id=1, name="钟楼", lat=34.2610, lon=108.9420,
             stay_min=60, score=7.5, ticket=30),
        Spot(source_id=2, name="回民街", lat=34.2650, lon=108.9350,
             stay_min=60, score=8.0, ticket=0, ticket_known=False),
        Spot(source_id=3, name="西安城墙", lat=34.2760, lon=108.9470,
             stay_min=90, score=8.5, ticket=54),
    ]
    task = MANAGER.create()
    task.status = "completed"
    task.req_params = {"city": "西安", "days": days,
                       "daily_start_h": 9.0, "daily_end_h": 18.0}
    task.request_spots = [s.model_dump() for s in spots]
    task.result = {
        "city": "西安",
        "days": [{"day": 1, "spots": [], "commute_min": 0.0,
                  "cost": 0.0, "active_min": 0.0}],
        "total_cost": 0.0, "cost_known": True, "total_score": 0.0,
        "unplanned": [], "check_report": {"passed": True, "violations": [],
                                          "warnings": [], "stats": {}},
    }
    return task


# ---------------------------------------------------------------- /plan/recheck

def _specs(*rows):
    """rows: (day, [(name, stay_min|None)...]) → 请求体 days"""
    out = []
    for day, items in rows:
        out.append({"day": day, "spots": [
            ({"name": n} if st is None else {"name": n, "stay_min": st})
            for n, st in items]})
    return out


def test_recheck_reorders_and_recomputes(monkeypatch, tmp_path):
    """换序 + 删点后：天数/成员正确，通勤与时间线是服务端算的（非客户端传值）。"""
    _no_amap(monkeypatch, tmp_path)
    task = _make_task()
    r = client.post("/plan/recheck", json={"task_id": task.id, "days": _specs(
        (1, [("回民街", None), ("钟楼", None)]))},   # 换序 + 去掉城墙
        headers={"X-App-Token": TOKEN})
    assert r.status_code == 200, r.text
    snap = r.json()
    d1 = snap["result"]["days"][0]
    names = [v["name"] for v in d1["spots"]]
    assert names == ["回民街", "钟楼"], "顺序必须与提交一致"
    assert d1["commute_min"] > 0 and d1["cost"] == 30.0
    # 时间线是服务端推的：首站到达 ≥ 出发时间 9:00，离 ≥ 到 + 停留
    assert d1["spots"][0]["arrive_h"] >= 9.0
    assert d1["spots"][0]["depart_h"] >= d1["spots"][0]["arrive_h"] + 1.0
    # 城墙没进 days → 进 unplanned
    assert any(u["name"] == "西安城墙" for u in snap["result"]["unplanned"])
    # 落盘：/task 磁盘兜底也能读到新结果
    from tasks import MANAGER
    assert MANAGER.get(task.id).result["days"][0]["spots"][0]["name"] == "回民街"


def test_recheck_stay_override_clamped(monkeypatch, tmp_path):
    """stay_min 覆盖生效并 clamp 进 [30,480]（客户端传 5 / 9999 都要收敛）。"""
    _no_amap(monkeypatch, tmp_path)
    task = _make_task()
    r = client.post("/plan/recheck", json={"task_id": task.id, "days": _specs(
        (1, [("钟楼", 5), ("回民街", 9999), ("西安城墙", None)]))},
        headers={"X-App-Token": TOKEN})
    assert r.status_code == 200, r.text
    spots = r.json()["result"]["days"][0]["spots"]
    by = {v["name"]: v for v in spots}
    if "钟楼" in by:                     # 5→30 分钟
        assert by["钟楼"]["depart_h"] - by["钟楼"]["arrive_h"] >= 0.49
    if "回民街" in by:                    # 9999→480 分钟：可能超窗被剔，剔了就进 unplanned
        assert (by["回民街"]["depart_h"] - by["回民街"]["arrive_h"]) <= 8.01


def test_recheck_rejects_unknown_and_duplicate(monkeypatch, tmp_path):
    """未知名字 / 同名排两次 → 400（成员校验是信任边界的第一道）。"""
    _no_amap(monkeypatch, tmp_path)
    task = _make_task()
    r = client.post("/plan/recheck", json={"task_id": task.id, "days": _specs(
        (1, [("兵马俑不存在", None)]))}, headers={"X-App-Token": TOKEN})
    assert r.status_code == 400 and "兵马俑不存在" in r.json()["detail"]
    r = client.post("/plan/recheck", json={"task_id": task.id, "days": _specs(
        (1, [("钟楼", None)]), (2, [("钟楼", None)]))},
        headers={"X-App-Token": TOKEN})
    assert r.status_code == 400 and "多次" in r.json()["detail"]


def test_recheck_requires_completed_task(monkeypatch, tmp_path):
    """任务不在内存/未完成 → 404（带可操作的提示，不是裸 500）。"""
    _no_amap(monkeypatch, tmp_path)
    r = client.post("/plan/recheck", json={"task_id": "deadbeef0000",
                                           "days": _specs((1, [("钟楼", None)]))},
                    headers={"X-App-Token": TOKEN})
    assert r.status_code == 404
    task = _make_task()
    task.status = "running"
    r = client.post("/plan/recheck", json={"task_id": task.id,
                                           "days": _specs((1, [("钟楼", None)]))},
                    headers={"X-App-Token": TOKEN})
    assert r.status_code == 404


# ---------------------------------------------------------------- /food

@pytest.fixture
def food_dir(monkeypatch, tmp_path):
    d = tmp_path / "food"
    monkeypatch.setattr(main, "FOOD_DIR", d)
    return d


def test_food_roundtrip_and_dedupe(food_dir, with_token):
    """加两条 → 同名更新 note（去重）→ 列表 2 条 → 删 1 条。"""
    h = {"X-App-Token": TOKEN}
    r = client.post("/food", json={"city": "西安", "name": "老米家",
                                   "note": "泡馍 28 元"}, headers=h)
    assert r.status_code == 200 and len(r.json()["entries"]) == 1
    # 同名再记：更新 note 而不是多一条
    r = client.post("/food", json={"city": "西安", "name": "老米家",
                                   "note": "泡馍涨价到 32"}, headers=h)
    assert len(r.json()["entries"]) == 1
    assert r.json()["entries"][0]["note"] == "泡馍涨价到 32"
    client.post("/food", json={"city": "西安", "name": "子午路张记",
                               "note": "肉夹馍"}, headers=h)
    r = client.get("/food", params={"city": "西安"}, headers=h)
    assert len(r.json()["entries"]) == 2
    r = client.post("/food/remove", json={"city": "西安", "name": "老米家"},
                    headers=h)
    assert [e["name"] for e in r.json()["entries"]] == ["子午路张记"]
    # 落盘文件在白名单城市名下
    assert (food_dir / "西安.json").exists()


def test_food_rejects_bad_city_and_oversize(food_dir, with_token):
    """非法城市名（路径穿越字符）→ 400，且不产生文件；超长字段 → 400。"""
    h = {"X-App-Token": TOKEN}
    for bad in ("../etc", "..\\x", "a" * 30):
        r = client.post("/food", json={"city": bad, "name": "x"}, headers=h)
        assert r.status_code == 400, bad
    r = client.post("/food", json={"city": "西安", "name": "y" * 41}, headers=h)
    assert r.status_code == 400
    assert list(food_dir.glob("*")) == [] if food_dir.exists() else True
    # GET 非法城市：空列表而不是 500
    r = client.get("/food", params={"city": "../x"}, headers=h)
    assert r.status_code == 200 and r.json()["entries"] == []


def test_food_write_follows_app_token_switch(food_dir, monkeypatch):
    """令牌开关语义与其他 POST 一致：未配 APP_TOKEN（本地开发）放行，配了才拦。

    「配了才拦」由 test_auth 的 401 断言统一覆盖（同一 verify_token 依赖），
    这里只锁「未配时写接口不误伤本地开发」这条容易被改坏的行为。
    """
    monkeypatch.setattr(main, "APP_TOKEN", "")
    r = client.post("/food", json={"city": "西安", "name": "本地加的",
                                   "note": "无需令牌"})
    assert r.status_code == 200
    assert r.json()["entries"][-1]["name"] == "本地加的"


# ---------------------------------------------------------------- /poi/search

def test_poi_search_gated_and_shaped(monkeypatch, with_token):
    """无令牌 401；有令牌打桩 text_search，返回最小字段形状。"""
    import editor
    monkeypatch.setattr(editor, "text_search",
                        lambda q, city, **k: [
                            {"name": "秦始皇兵马俑博物馆", "lat": 34.3847,
                             "lon": 109.2785, "type_str": "风景名胜;博物馆",
                             "rating": "5", "junk": "不该返回"}])
    r = client.get("/poi/search", params={"q": "兵马俑"})
    assert r.status_code == 401
    r = client.get("/poi/search", params={"q": "兵马俑", "city": "西安"},
                   headers={"X-App-Token": TOKEN})
    assert r.status_code == 200
    rows = r.json()["results"]
    assert rows[0]["name"] == "秦始皇兵马俑博物馆"
    assert "junk" not in rows[0] and "lat" in rows[0]
    # 空 query → 400
    r = client.get("/poi/search", params={"q": " "},
                   headers={"X-App-Token": TOKEN})
    assert r.status_code == 400


# ---------------------------------------------------------------- /food/recommend

def test_food_recommend_is_food_only(food_dir, with_token, monkeypatch):
    """纯美食契约：列表只出内置特色菜名（**不出饭店**），高德仅作为配图来源。"""
    import editor

    def fake_ts(query, city, **kw):
        # 按菜名搜代表店 → 返回带图的同名餐饮 POI（配图用途）
        return [{"name": query + "（代表店）", "image": "http://img/" +
                 str(abs(hash(query)))[:6] + ".jpg",
                 "type_str": "餐饮服务;地方菜", "address": "某路 1 号",
                 "rating": "4.5"},
                {"name": "停车场旁超市", "type_str": "购物服务;超市"}]

    monkeypatch.setattr(editor, "text_search", fake_ts)
    r = client.get("/food/recommend", params={"city": "西安"},
                   headers={"X-App-Token": TOKEN})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["cached"] is False
    # 1) 全部是内置特色 —— 没有任何饭店条目混入
    assert all(x["source"] == "内置特色" for x in d["items"])
    # 2) 菜名来自主种子表（西安 8 道）
    names = [x["name"] for x in d["items"]]
    assert names[:2] == ["羊肉泡馍", "肉夹馍"] and len(names) == 8
    # 3) 配图来自高德（搜到同名店）；搜图时做了餐饮过滤（假 POI 的 type 是餐饮）
    assert all(x["image"].startswith("http://img/") for x in d["items"])
    # 4) 缓存后不再打上游
    calls = []
    monkeypatch.setattr(editor, "text_search",
                        lambda *a, **k: calls.append(1) or [])
    r = client.get("/food/recommend", params={"city": "西安"},
                   headers={"X-App-Token": TOKEN})
    assert r.json()["cached"] is True and calls == []


def test_food_recommend_upstream_down_keeps_seeds(food_dir, with_token, monkeypatch):
    """上游完全不可用：种子照常返回（无图走首字占位），不编造图片。"""
    import editor
    monkeypatch.setattr(editor, "text_search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("配额耗尽")))
    r = client.get("/food/recommend", params={"city": "西安"},
                   headers={"X-App-Token": TOKEN})
    d = r.json()
    assert len(d["items"]) == 8
    assert all(x["source"] == "内置特色" for x in d["items"])
    assert all(x["image"] == "" for x in d["items"])


def test_food_old_list_file_still_readable(food_dir, with_token):
    """旧版裸 list 美食文件：entries 读得出、recommend 接口不炸、追加保持结构。"""
    import json as _json
    food_dir.mkdir(parents=True, exist_ok=True)
    (food_dir / "成都.json").write_text(
        _json.dumps([{"name": "冒节子", "note": "肥肠粉标配"}], ensure_ascii=False),
        encoding="utf-8")
    h = {"X-App-Token": TOKEN}
    r = client.get("/food", params={"city": "成都"}, headers=h)
    assert r.json()["entries"][0]["name"] == "冒节子"
    r = client.post("/food", json={"city": "成都", "name": "蛋烘糕", "note": "甜口"},
                    headers=h)
    names = [e["name"] for e in r.json()["entries"]]
    assert names == ["冒节子", "蛋烘糕"]


