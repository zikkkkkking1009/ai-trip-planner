"""weather.py 的单元测试。

**全部不联网**：CI 环境可能没有外网，而且真实天气每天都在变 —— 断言一旦依赖真实响应
就会变成随机失败。所以凡是外部调用一律 monkeypatch 掉 `weather._fetch`。
只有纯函数（映射 / 数据源选择 / 响应解析）与降级路径在这里被验证。
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlparse

import pytest

import weather


def _resp(dates, codes=None, tmax=None, tmin=None, precip=None, prob=None, wind=None):
    """构造 open-meteo 的「列式」响应（它按字段给数组，不是按天给对象）。"""
    n = len(dates)
    daily = {
        "time": list(dates),
        "weather_code": codes if codes is not None else [0] * n,
        "temperature_2m_max": tmax if tmax is not None else [25.0] * n,
        "temperature_2m_min": tmin if tmin is not None else [15.0] * n,
        "precipitation_sum": precip if precip is not None else [0.0] * n,
        "wind_speed_10m_max": wind if wind is not None else [8.0] * n,
    }
    if prob is not None:
        daily["precipitation_probability_max"] = prob
    return {"daily": daily}


@pytest.fixture(autouse=True)
def _clear_cache():
    """进程内缓存是模块级的，用例之间必须隔离，否则会互相串结果。"""
    weather._cache.clear()
    yield
    weather._cache.clear()


class TestDescribeCode:
    def test_known_codes(self):
        assert weather.describe_code(0) == ("晴", "☀️")
        assert weather.describe_code(63)[0] == "中雨"

    def test_unknown_code_is_admitted_not_guessed(self):
        """未知 code 必须明说「未知」，不能猜一个像样的天气。

        这正是本项目最忌讳的静默错误：编一个看起来合理的值，比明说不知道更糟。
        """
        assert weather.describe_code(999)[0] == "未知"
        assert weather.describe_code(None)[0] == "未知"


class TestPickSource:
    T = date(2026, 9, 23)

    def test_future_uses_forecast(self):
        src, why = weather.pick_source(self.T, self.T + timedelta(days=2), self.T)
        assert src == "forecast" and why == ""

    def test_near_past_uses_archive(self):
        src, _ = weather.pick_source(self.T - timedelta(days=3),
                                     self.T - timedelta(days=1), self.T)
        assert src == "archive"

    def test_range_covering_today_uses_forecast(self):
        src, _ = weather.pick_source(self.T - timedelta(days=1),
                                     self.T + timedelta(days=1), self.T)
        assert src == "forecast"

    def test_beyond_forecast_window_says_why(self):
        src, why = weather.pick_source(self.T + timedelta(days=40),
                                       self.T + timedelta(days=42), self.T)
        assert src is None and "超出预报范围" in why

    def test_too_old_says_why(self):
        src, why = weather.pick_source(self.T - timedelta(days=60),
                                       self.T - timedelta(days=58), self.T)
        assert src is None and "太久远" in why

    def test_reversed_range(self):
        src, why = weather.pick_source(self.T, self.T - timedelta(days=1), self.T)
        assert src is None and "不合法" in why


class TestParse:
    def test_filters_to_requested_range(self):
        """接口可能多返回几天，必须裁到行程范围内 —— 否则会出现「行程外的天气」。"""
        data = _resp(["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"])
        days = weather._parse(data, date(2026, 9, 23), date(2026, 9, 24), "forecast")
        assert [d["date"] for d in days] == ["2026-09-23", "2026-09-24"]

    def test_null_values_become_none(self):
        """open-meteo 对缺测值给 null —— 直接 float() 会抛异常，必须容错成 None。"""
        data = _resp(["2026-09-23"], tmax=[None], tmin=[None], prob=[None])
        d = weather._parse(data, date(2026, 9, 23), date(2026, 9, 23), "forecast")[0]
        assert d["t_max"] is None
        assert d["t_min"] is None
        assert d["precip_prob"] is None

    def test_archive_without_probability_key(self):
        """归档响应没有降水概率字段：不能因此炸掉，也不能假装它是 0。"""
        data = _resp(["2026-09-23"], precip=[1.2])
        d = weather._parse(data, date(2026, 9, 23), date(2026, 9, 23), "archive")[0]
        assert d["precip_prob"] is None
        assert d["precip_mm"] == 1.2

    def test_chinese_weekday(self):
        # 2026-09-24 是周四
        data = _resp(["2026-09-24"])
        d = weather._parse(data, date(2026, 9, 24), date(2026, 9, 24), "forecast")[0]
        assert d["weekday"] == "周四"

    def test_bad_date_string_is_skipped_not_crash(self):
        data = _resp(["not-a-date", "2026-09-24"])
        days = weather._parse(data, date(2026, 9, 23), date(2026, 9, 25), "forecast")
        assert [d["date"] for d in days] == ["2026-09-24"]

    def test_empty_daily_gives_empty_list(self):
        assert weather._parse({}, date(2026, 9, 23), date(2026, 9, 24), "forecast") == []


class TestRequestUrl:
    """守住「两个数据源域名与参数不同」这件事 —— 写错域名会直接 404。

    注意这里断言的是 **host**（用 urlparse 取 netloc）而不是子串包含：
    `archive-api.open-meteo.com` 本身就包含子串 `api.open-meteo.com`，
    拿字符串 in/not in 去比会永远通过，等于没测。
    """

    def test_forecast_url(self):
        u = weather._build_url("forecast", 1.0, 2.0,
                               date(2026, 9, 1), date(2026, 9, 2))
        assert urlparse(u).netloc == "api.open-meteo.com"
        assert urlparse(u).path == "/v1/forecast"
        assert "precipitation_probability_max" in u

    def test_archive_url_uses_separate_host_and_omits_probability(self):
        """归档在独立的 archive-api 子域；且它不认降水概率字段，带上会导致整体失败。

        （写这条是因为实现时我真的把域名写成了 api.open-meteo.com/v1/archive，
        实测 404 —— 靠一次真实调用才发现。）
        """
        u = weather._build_url("archive", 1.0, 2.0,
                               date(2026, 9, 1), date(2026, 9, 2))
        assert urlparse(u).netloc == "archive-api.open-meteo.com"
        assert urlparse(u).path == "/v1/archive"
        assert "precipitation_probability_max" not in u
        assert "precipitation_sum" in u


class TestDailyWeather:
    T = date(2026, 9, 23)

    def test_unknown_city_is_admitted(self):
        """未知城市必须明说，绝不能偷偷换一个城市的坐标去查（项目踩过的静默错误）。"""
        r = weather.daily_weather("火星", self.T, self.T + timedelta(days=2), today=self.T)
        assert r["available"] is False
        assert "未知城市" in r["reason"]

    def test_out_of_range_is_admitted(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(weather, "_fetch",
                            lambda url: called.__setitem__("n", called["n"] + 1) or {})
        r = weather.daily_weather("西安", self.T + timedelta(days=40),
                                  self.T + timedelta(days=42), today=self.T)
        assert r["available"] is False and "超出预报范围" in r["reason"]
        assert called["n"] == 0, "明确拿不到时不应发起任何请求"

    def test_network_failure_degrades_without_raising(self, monkeypatch):
        def boom(url):
            raise OSError("network down")
        monkeypatch.setattr(weather, "_fetch", boom)
        r = weather.daily_weather("西安", self.T + timedelta(days=1),
                                  self.T + timedelta(days=2), today=self.T)
        assert r["available"] is False
        assert "不可用" in r["reason"]

    def test_success_shape(self, monkeypatch):
        monkeypatch.setattr(weather, "_fetch",
                            lambda url: _resp(["2026-09-24", "2026-09-25"],
                                              codes=[61, 0], prob=[80, 10]))
        r = weather.daily_weather("西安", self.T + timedelta(days=1),
                                  self.T + timedelta(days=2), today=self.T)
        assert r["available"] is True
        assert r["source"] == "open-meteo 预报"
        assert len(r["days"]) == 2
        assert r["days"][0]["text"] == "小雨"
        assert r["days"][0]["precip_prob"] == 80

    def test_empty_days_reports_unavailable(self, monkeypatch):
        """响应里没有对应日期的记录时，必须说没有，不能给一个空列表当成功。"""
        monkeypatch.setattr(weather, "_fetch", lambda url: _resp(["1999-01-01"]))
        r = weather.daily_weather("西安", self.T + timedelta(days=1),
                                  self.T + timedelta(days=2), today=self.T)
        assert r["available"] is False and "没有返回" in r["reason"]

    def test_second_call_hits_cache(self, monkeypatch):
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return _resp(["2026-09-24"])
        monkeypatch.setattr(weather, "_fetch", fake)
        a = weather.daily_weather("西安", self.T + timedelta(days=1),
                                  self.T + timedelta(days=1), today=self.T)
        b = weather.daily_weather("西安", self.T + timedelta(days=1),
                                  self.T + timedelta(days=1), today=self.T)
        assert a == b
        assert calls["n"] == 1, "第二次必须命中缓存（限频/降本）"

    def test_cache_is_keyed_by_city(self, monkeypatch):
        """缓存键必须含城市 —— 否则先查的城市会把结果串给后查的城市。"""
        monkeypatch.setattr(weather, "_fetch", lambda url: _resp(["2026-09-24"]))
        weather.daily_weather("西安", self.T + timedelta(days=1),
                              self.T + timedelta(days=1), today=self.T)
        weather.daily_weather("成都", self.T + timedelta(days=1),
                              self.T + timedelta(days=1), today=self.T)
        keys = {k[0] for k in weather._cache}
        assert keys == {"西安", "成都"}
