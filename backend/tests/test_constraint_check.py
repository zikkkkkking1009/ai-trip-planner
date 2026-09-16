"""约束校验器单元测试。"""
from models import DayPlan, PlanRequest, VisitedSpot
from constraint_check import check_plan


def _req(budget: float | None = 100) -> PlanRequest:
    return PlanRequest(city="西安", days=1, budget=budget, spots=[])


def _day(day: int, spots: list[VisitedSpot], commute: float = 30.0) -> DayPlan:
    return DayPlan(day=day, spots=spots, commute_min=commute,
                   cost=sum(v.ticket for v in spots),
                   active_min=sum(v.depart_h - v.arrive_h for v in spots) * 60)


def test_budget_violation_is_reported():
    req = _req(budget=100)
    days = [_day(1, [VisitedSpot(name="A", arrive_h=9, depart_h=11, ticket=150)])]
    report = check_plan(req, days, total_cost=150)
    assert not report["passed"]
    assert any("预算" in v for v in report["violations"])


def test_clean_plan_passes():
    req = _req(budget=500)
    days = [_day(1, [VisitedSpot(name="A", arrive_h=9, depart_h=11, ticket=50)])]
    report = check_plan(req, days, total_cost=50)
    assert report["passed"]
    assert report["violations"] == []


def test_empty_day_gets_warning():
    req = _req(budget=None)
    days = [_day(1, []), _day(2, [])]
    report = check_plan(req, days, total_cost=0)
    assert report["passed"]  # 空天是警告不是违规
    assert any("没有安排" in w for w in report["warnings"])
