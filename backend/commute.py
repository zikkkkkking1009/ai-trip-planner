"""通勤矩阵：高德路径数据 + 本地缓存 + 自动降级。

设计（对应《含金量提升方案》硬菜一的配套）：
1. 有 AMAP_KEY → 调高德「距离测量」API（/v3/distance，type=1 驾车）拿真实时长
2. 无 Key / 调用失败 → 自动降级回 haversine 估算，服务不中断
3. 所有结果落 JSON 缓存（坐标取 3 位小数 ≈110m 粒度，提高命中率）
4. 全程统计 api_calls / cache_hits / fallbacks——这三个数就是简历素材
   （「缓存使高德 API 调用量下降 XX%」）

Key 配置：backend/.env 里写 AMAP_KEY=xxx（文件已 gitignore）。
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from models import Spot
from solver import COMMUTE_OVERHEAD_MIN, haversine_km

BACKEND_DIR = Path(__file__).parent
CACHE_FILE = BACKEND_DIR / ".commute_cache.json"
ENV_FILE = BACKEND_DIR / ".env"

# 近似缓存 TTL：7 天（交通时长会变，但不能太短，否则缓存没意义）
CACHE_TTL_SEC = 7 * 24 * 3600


def load_env_file() -> dict:
    """极简 .env 解析（不引入 python-dotenv 依赖）。"""
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _estimate_min(a: Spot, b: Spot) -> float:
    """降级用的估算：haversine / 市内均速 + 固定开销。"""
    km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    return max(10.0, km / 18.0 * 60 + COMMUTE_OVERHEAD_MIN)


def _cache_key(a: Spot, b: Spot) -> str:
    """坐标取 3 位小数（~110m）做 key，同区域重复请求直接命中。"""
    return (f"{round(a.lat, 3):.3f},{round(a.lon, 3):.3f}|"
            f"{round(b.lat, 3):.3f},{round(b.lon, 3):.3f}")


class CommuteMatrix:
    """带缓存的高德通勤时长查询器。

    三级查找：进程内 memo → 文件缓存 → 高德 API（带限频节流）。
    个人 Key 的距离测量接口限频约 3 QPS，不节流会被批量拒绝。
    """

    QPS_MIN_INTERVAL = 0.35  # 秒，两次真实 API 调用之间的最小间隔

    def __init__(self, amap_key: str | None = None):
        env = load_env_file()
        self.key = amap_key or env.get("AMAP_KEY") or os.environ.get("AMAP_KEY")
        self.cache: dict[str, dict] = {}
        self._mem: dict[str, float] = {}   # 进程内去重：同一对景点只算一次
        self._last_call = 0.0
        if CACHE_FILE.exists():
            try:
                self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.cache = {}
        self.stats = {"api_calls": 0, "cache_hits": 0, "fallbacks": 0}

    # ---- 高德 API ----
    def _amap_driving_min(self, a: Spot, b: Spot) -> float:
        """调 /v3/distance 拿驾车时长（分钟）。失败抛异常，由调用方降级。"""
        params = urllib.parse.urlencode({
            "origins": f"{a.lon},{a.lat}",   # 高德参数顺序是 经度,纬度
            "destination": f"{b.lon},{b.lat}",
            "type": 1,                        # 1 = 驾车距离与时长
            "key": self.key,
        })
        url = f"https://restapi.amap.com/v3/distance?{params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "1" or not data.get("results"):
            raise RuntimeError(f"高德返回异常: {data.get('info')}")
        r = data["results"][0]
        return float(r["duration"]) / 60.0    # 秒 → 分钟

    # ---- 对外主入口 ----
    def minutes(self, a: Spot, b: Spot) -> float:
        """两景点间通勤分钟数。memo → 文件缓存 → 高德 API → 估算降级。"""
        if a is b or (a.lat, a.lon) == (b.lat, b.lon):
            return 0.0

        ck = _cache_key(a, b)
        if ck in self._mem:
            self.stats["cache_hits"] += 1
            return self._mem[ck]

        hit = self.cache.get(ck)
        if hit and time.time() - hit["ts"] < CACHE_TTL_SEC:
            self.stats["cache_hits"] += 1
            self._mem[ck] = float(hit["min"])
            return self._mem[ck]

        if self.key:
            try:
                self._throttle()
                minutes = self._amap_driving_min(a, b) + 5.0  # 停车/进站余量
                self.stats["api_calls"] += 1
                self.cache[ck] = {"min": round(minutes, 1), "ts": time.time()}
                self._mem[ck] = minutes
                self._flush()
                return minutes
            except Exception:
                self.stats["fallbacks"] += 1

        minutes = _estimate_min(a, b)
        self._mem[ck] = minutes  # 估算也 memo，避免同一对反复降级
        return minutes

    def _throttle(self) -> None:
        """限频：距离测量接口个人 Key 约 3 QPS。"""
        wait = self.QPS_MIN_INTERVAL - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _flush(self) -> None:
        CACHE_FILE.write_text(
            json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    def hit_rate(self) -> float:
        total = self.stats["cache_hits"] + self.stats["api_calls"]
        return self.stats["cache_hits"] / total if total else 0.0

    # ---- 批量预计算（异步任务的进度展示用）----
    def precompute(self, spots: list[Spot],
                   progress_cb=None) -> int:
        """把所有景点两两之间的通勤提前算好（命中缓存则零开销）。

        N 个景点是 N(N-1)/2 对；首次会集中调高德 API（受 QPS 节流），
        之后全部走缓存。progress_cb(done, total) 供进度推送。
        返回真实发起的 API 调用次数。
        """
        uniq: list[Spot] = []
        seen: set[tuple[float, float]] = set()
        for s in spots:
            key = (s.lat, s.lon)
            if key not in seen:
                seen.add(key)
                uniq.append(s)

        pairs = [(uniq[i], uniq[j])
                 for i in range(len(uniq)) for j in range(i + 1, len(uniq))]
        before = self.stats["api_calls"]
        for idx, (a, b) in enumerate(pairs):
            self.minutes(a, b)  # 同时缓存正反两个方向
            self.minutes(b, a)
            if progress_cb and (idx % 5 == 0 or idx == len(pairs) - 1):
                progress_cb(idx + 1, len(pairs))
        return self.stats["api_calls"] - before
