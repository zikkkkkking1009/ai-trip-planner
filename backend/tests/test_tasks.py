"""异步任务系统测试：提交 → 进度 → 完成（不依赖网络，走缓存/估算）。"""
import time

from fastapi.testclient import TestClient

from main import app
from models import Spot


def _small_req() -> dict:
    spots = [
        Spot(source_id=1, name="钟楼", lat=34.2610, lon=108.9420,
             stay_min=60, score=7.5, ticket=30),
        Spot(source_id=2, name="碑林博物馆", lat=34.2520, lon=108.9470,
             stay_min=90, score=7.4, ticket=50),
        Spot(source_id=3, name="回民街", lat=34.2650, lon=108.9350,
             stay_min=90, score=8.0),
        Spot(source_id=4, name="西安城墙", lat=34.2760, lon=108.9470,
             stay_min=90, score=8.5, ticket=54),
    ]
    return {"city": "西安", "days": 1, "budget": None, "spots":
            [s.model_dump() for s in spots]}


def test_async_task_full_lifecycle():
    # 用 with 上下文让 TestClient 的 event loop 跨请求存活——
    # 否则后台任务随请求结束被丢弃，任务永远停在 running
    # （CI Linux 上必现，本地 Windows 因时序差异偶尔能过——经典环境差异 bug）
    with TestClient(app) as client:
        # 1) 提交异步任务
        r = client.post("/plan/async", json=_small_req())
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] and body["ws_url"].startswith("/ws/")

        # 2) 轮询直到完成（无 AMAP_KEY 时走估算降级，应当秒级完成）
        deadline = time.time() + 120
        while time.time() < deadline:
            snap = client.get(f"/task/{body['task_id']}").json()
            if snap["status"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        assert snap["status"] == "completed", snap.get("error")

        # 3) 进度日志覆盖主要阶段
        stages = {p["stage"] for p in snap["progress"]}
        assert {"启动", "通勤矩阵", "构造", "完成"} <= stages

        # 4) 结果结构与同步接口一致
        result = snap["result"]
        assert result["days"] and result["days"][0]["spots"]
        assert "check_report" in result


def test_hotel_passed_as_plan_input():
    """酒店可以「先选好再规划」：入参里的 hotel 必须进入结果。

    回归点：tasks.run_plan_task 原先把 req_params["hotel"] 写死成 None，
    导致 PlanRequest.hotel 形同虚设 —— 前端点了选酒店没反应，
    只能"先规划一次、再用 /hotel/set 换酒店"。
    """
    req = _small_req()
    req["hotel"] = {"name": "测试住宿·钟楼店", "lat": 34.2600, "lon": 108.9430, "desc": ""}
    with TestClient(app) as client:
        r = client.post("/plan/async", json=req)
        assert r.status_code == 200, r.text
        body = r.json()

        deadline = time.time() + 120
        while time.time() < deadline:
            snap = client.get(f"/task/{body['task_id']}").json()
            if snap["status"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        assert snap["status"] == "completed", snap.get("error")

        hotel = (snap["result"] or {}).get("hotel")
        assert hotel, "规划入参里的酒店没进结果 —— hotel 未透传给求解器"
        assert hotel["name"] == "测试住宿·钟楼店"


def test_task_not_found():
    client = TestClient(app)
    assert client.get("/task/nonexistent").status_code == 404


def test_task_manager_evicts_when_over_capacity():
    """容量保护：超出上限时淘汰最旧的终态任务，运行中的任务不受影响。"""
    from tasks import TaskManager
    mgr = TaskManager()
    mgr.MAX_TASKS = 5
    running = mgr.create()
    running.status = "running"
    for i in range(10):
        t = mgr.create()
        t.status = "completed"
        t.created_at = 1000 + i          # 手工拉开创建时间
    assert len(mgr._tasks) <= 5
    assert mgr.get(running.id) is not None, "运行中的任务不应被淘汰"
    assert mgr.evicted > 0


def test_task_manager_ttl_evicts_finished():
    """TTL 保护：终态且超龄的任务被清理。"""
    from tasks import TaskManager
    mgr = TaskManager()
    old = mgr.create()
    old.status = "completed"
    old.created_at = 0                    # 远古任务
    mgr.create()                          # 触发惰性清理
    assert mgr.get(old.id) is None


def test_spawn_marks_failed_on_unhandled_exception():
    """spawn 托管的后台协程抛异常时，任务必须被置为 failed，不能永远停在 running。

    回归：裸 create_task 的返回值没人引用（可能被 GC），协程异常也没人收 ——
    两条路都会造出「僵尸 running」：WS 客户端空转不退出、任务永不淘汰。
    """
    import asyncio

    from tasks import MANAGER

    async def boom():
        raise RuntimeError("炸了")

    async def driver():
        t = MANAGER.create()
        t.status = "running"
        result["task"] = t
        MANAGER.spawn(t.id, boom())
        await asyncio.sleep(0.05)         # 让后台任务跑完、done 回调触发

    result: dict = {}
    asyncio.run(driver())
    task = result["task"]
    assert task.status == "failed", f"僵尸 running：status={task.status}"
    assert "炸了" in (task.error or "")


# ---------------------------------------------------------------- 编辑任务（run_edit_task / run_set_hotel）

def _no_amap(monkeypatch, tmp_path):
    """测试期掐掉高德 Key 与磁盘缓存。

    conftest 只掐了 selftest 的探测；本机 backend/.env 里有真 Key，而
    main.py import 时会把它灌进 os.environ —— 不在这里显式掐掉，
    run_edit_task / run_set_hotel 里的 CommuteMatrix 会真发请求、真花钱，
    还会把测试数据写进真实的 .commute_cache.json。
    """
    import commute as commute_mod
    monkeypatch.setattr(commute_mod, "load_env_file", lambda: {})
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.setattr(commute_mod, "CACHE_FILE", tmp_path / "commute_cache.json")


def _base_task_with_result():
    """构造一个已完成的基准任务：1 天 2 景点（通勤走估算，零外呼）。"""
    from tasks import MANAGER
    spots = [
        Spot(source_id=1, name="钟楼", lat=34.2610, lon=108.9420,
             stay_min=60, score=7.5, ticket=30),
        Spot(source_id=2, name="回民街", lat=34.2650, lon=108.9350,
             stay_min=60, score=8.0),
    ]
    base = MANAGER.create()
    base.status = "completed"
    base.req_params = {"city": "西安", "days": 1,
                       "daily_start_h": 9.0, "daily_end_h": 18.0}
    base.request_spots = [s.model_dump() for s in spots]
    base.result = {
        "city": "西安",
        "days": [{"day": 1, "spots": [s.model_dump() for s in spots],
                  "commute_min": 10.0, "cost": 30.0, "active_min": 120.0}],
        "total_cost": 30.0, "total_score": 15.5, "unplanned": [],
        "check_report": {"passed": True, "violations": [], "warnings": [],
                         "stats": {}},
    }
    return base


def test_edit_task_does_not_mutate_base_req_params(monkeypatch, tmp_path):
    """回归：编辑任务不得改写基准任务的入参。

    旧实现 `params = base.req_params` 拿的是引用，hotel/preference 直接写进
    基准任务 —— 基准任务快照里出现不属于它的酒店/偏好，同一 base 并发两次
    编辑互相覆盖。修后必须 deepcopy，基准任务原封不动。
    """
    import asyncio
    import editor
    import main
    from tasks import MANAGER, run_edit_task

    _no_amap(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "PLANS_DIR", tmp_path / "plans")
    base = _base_task_with_result()
    # tasks.py 在函数内延迟 `import editor`，拿到的是同一个模块对象，
    # 直接 patch editor 模块即可生效
    monkeypatch.setattr(
        editor, "parse_instruction",
        lambda *a, **k: {"ops": [{"op": "hotel", "query": "钟楼酒店"},
                                 {"op": "pref", "value": "less_walk"}],
                         "reply": "好的"})
    monkeypatch.setattr(
        editor, "text_search",
        lambda query, city, **k: [{"name": "测试酒店", "lat": 34.2600,
                                   "lon": 108.9430, "type_str": "住宿服务;宾馆酒店"}])

    task = MANAGER.create()
    asyncio.run(run_edit_task(task.id, base.id, "住钟楼附近，少走路"))

    assert task.status == "completed", task.error
    assert "hotel" not in base.req_params, "基准任务的入参被编辑任务改写了"
    assert "preference" not in base.req_params, "基准任务的入参被编辑任务改写了"
    assert task.req_params["hotel"]["name"] == "测试酒店"
    assert task.req_params["preference"] == "less_walk"
    # 回归：apply_ops 的变更说明此前整体覆盖了 hotel 操作累计的 changes，
    # 用户看不到「住宿设为X」—— 必须**追加**而不是重新赋值
    joined = "；".join(task.result["changes"])
    assert "住宿设为「测试酒店」" in joined, f"hotel 变更说明丢失：{joined}"


def test_edit_pin_add_writes_history_and_snapshot(monkeypatch, tmp_path):
    """回归：pin_add 轻量路径成功后必须写 chat_history 并落盘快照。

    这条是「加个咖啡馆」的主路径，此前 return 前漏了收尾：
    TTL 淘汰/重启后 /task/{id} 404，下一轮编辑丢全部对话记忆。
    """
    import asyncio
    import editor
    import main
    from tasks import MANAGER, run_edit_task

    _no_amap(monkeypatch, tmp_path)
    plans = tmp_path / "plans"
    monkeypatch.setattr(main, "PLANS_DIR", plans)
    base = _base_task_with_result()
    monkeypatch.setattr(
        editor, "parse_instruction",
        lambda *a, **k: {"ops": [{"op": "pin_add", "name": "测试咖啡馆"}],
                         "reply": "已加入"})
    monkeypatch.setattr(
        editor, "poi_search",
        lambda query, lat, lon, **k: [{"name": "测试咖啡馆", "lat": 34.2620,
                                       "lon": 108.9400,
                                       "type_str": "餐饮服务;咖啡馆"}])

    task = MANAGER.create()
    asyncio.run(run_edit_task(task.id, base.id, "加个咖啡馆"))

    assert task.status == "completed", task.error
    assert task.chat_history, "轻量路径必须写对话记忆"
    assert task.chat_history[-1]["q"] == "加个咖啡馆"
    assert (plans / f"{task.id}.json").exists(), "轻量路径必须落盘快照"


def test_set_hotel_completes_and_keeps_base_intact(monkeypatch, tmp_path):
    """换酒店：新任务带新酒店，基准任务入参不动；预热在线程里跑不冻事件循环。"""
    import asyncio
    import main
    from tasks import MANAGER, run_set_hotel

    _no_amap(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "PLANS_DIR", tmp_path / "plans")
    base = _base_task_with_result()

    task = MANAGER.create()
    asyncio.run(run_set_hotel(task.id, base.id,
                              {"name": "测试酒店", "lat": 34.2600, "lon": 108.9430}))

    assert task.status == "completed", task.error
    assert task.req_params["hotel"]["name"] == "测试酒店"
    assert "hotel" not in base.req_params, "基准任务的入参被换酒店改写了"
