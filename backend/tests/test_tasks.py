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
