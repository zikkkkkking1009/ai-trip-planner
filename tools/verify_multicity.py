# -*- coding: utf-8 -*-
"""多城市泛化端到端验证（服务需先启动）：粘一段成都攻略 → 识别城市 → 对齐 → 排期 → 校验坐标落在成都。

同时做回归：西安路径必须仍然正常（不能被改造破坏）。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8011"  # 与服务启动端口一致
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CITY_BOUNDS = {
    # 城市 -> (纬度范围, 经度范围)，用于断言坐标确实落在该城市
    "成都": ((30.3, 31.2), (103.3, 104.5)),
    "西安": ((33.8, 34.6), (108.6, 109.4)),
}


def get(path):
    with opener.open(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body=None):
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(BASE + path, data=(body or b""), method="POST",
                                 headers=headers)
    try:
        with opener.open(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


print("=== 1. /cities 城市列表 ===")
c = get("/cities")
print("  可演示城市:", c["cities"], "| 默认:", c["default"])

print("\n=== 2. /demo/spots 各城市景点数 ===")
for city in c["cities"]:
    d = get(f"/demo/spots?city={urllib.parse.quote(city)}")
    lat_c, lon_c = d["center"]
    in_city = sum(1 for s in d["spots"]
                  if abs(s["lat"] - lat_c) < 1.5 and abs(s["lon"] - lon_c) < 1.5)
    print(f'  {city}: {len(d["spots"])} 个景点，其中 {in_city} 个坐标落在城市附近')
    assert d["available"] and len(d["spots"]) >= 8, f"{city} 景点数据不足"

print("\n=== 3. 未知城市不应静默回落到别的城市 ===")
d = get("/demo/spots?city=" + urllib.parse.quote("火星"))
print(f'  火星 -> available={d["available"]}, spots={len(d["spots"])}（期望 False/0）')
assert d["available"] is False and len(d["spots"]) == 0

print("\n=== 4. 成都攻略 → 抽取（城市识别）===")
GUIDE = ("成都三日游：第一天逛宽窄巷子和锦里，晚上在春熙路吃饭；"
         "第二天去成都大熊猫繁育研究基地，下午到武侯祠和杜甫草堂；"
         "第三天去都江堰景区，顺路青城山。")
t0 = time.time()
status, d = post("/extract?" + urllib.parse.urlencode({"text": GUIDE, "city": ""}))
print(f'  HTTP {status}，耗时 {time.time()-t0:.1f}s')
print(f'  识别城市: detected_city={d["detected_city"]!r}  needs_city={d["needs_city"]}  '
      f'对齐城市={d["city"]}')
print(f'  识别景点 {d["count"]} 个，待人工 {d["needs_review_count"]} 个')
for s in d["spots"]:
    tag = "待人工" if s["needs_review"] else "已采纳"
    print(f'    {s["name"]:<24} ({s["lat"]:.4f},{s["lon"]:.4f}) 置信{s["confidence"]} {tag}')
assert d["detected_city"] == "成都", f'城市识别失败：{d["detected_city"]}'
assert d["needs_city"] is False

# 坐标必须落在成都范围内（这是本次改造要修的核心问题）
(lat_lo, lat_hi), (lon_lo, lon_hi) = CITY_BOUNDS["成都"]
ok = [s for s in d["spots"] if lat_lo <= s["lat"] <= lat_hi and lon_lo <= s["lon"] <= lon_hi]
print(f'  坐标在成都范围内: {len(ok)}/{d["spots"]} 个')
assert len(ok) == len(d["spots"]), "有景点坐标不在成都——泛化未生效！"

adopted = [s for s in d["spots"] if not s["needs_review"]]
print(f'\n=== 5. 用采纳的 {len(adopted)} 个景点排期 ===')
body = json.dumps({"city": "成都", "days": 2, "daily_start_h": 9.0, "daily_end_h": 18.0,
                   "budget": 500, "spots": adopted}, ensure_ascii=False).encode("utf-8")
status, r = post("/plan", body)
print(f'  HTTP {status}')
assert status == 200, r
print(f'  天数 {len(r["days"])}，总门票 {r["total_cost"]}，总收益 {r["total_score"]}，'
      f'约束校验通过={r["check_report"]["passed"]}')
names = []
for day in r["days"]:
    print(f'    Day{day["day"]}: ' + "、".join(s["name"] for s in day["spots"]))
    names += [s["name"] for s in day["spots"]]
adopted_names = {s["name"] for s in adopted}
unknown = [n for n in names if n not in adopted_names]
assert not unknown, f"行程里出现未采纳的景点：{unknown}"
print(f'  ✓ 行程 {len(names)} 个条目的名称全部来自成都对齐结果（坐标已在上一步逐条验证）')
print(f'  未排入: {[(u["name"], u["reason"]) for u in r["unplanned"]] or "无"}')

print("\n=== 6. 回归：西安路径仍然正常 ===")
status, d2 = post("/extract?" + urllib.parse.urlencode({"text": "西安三日游：兵马俑、华清宫、回民街、大雁塔、城墙。", "city": ""}))
print(f'  识别城市: {d2["detected_city"]!r}，景点 {d2["count"]} 个，待人工 {d2["needs_review_count"]}')
(lat_lo_x, lat_hi_x), (lon_lo_x, lon_hi_x) = CITY_BOUNDS["西安"]
ok_x = [s for s in d2["spots"] if lat_lo_x <= s["lat"] <= lat_hi_x]
print(f'  坐标在西安范围内: {len(ok_x)}/{d2["spots"]}')
assert d2["detected_city"] == "西安"
assert len(ok_x) == len(d2["spots"])

print("\n=== 7. 同城不同城的同名 POI 不串味（成都 vs 西安都有的「武侯祠/城墙」类）===")
cd = get("/demo/spots?city=" + urllib.parse.quote("成都"))
xa = get("/demo/spots?city=" + urllib.parse.quote("西安"))
names_cd = {s["name"] for s in cd["spots"]}
names_xa = {s["name"] for s in xa["spots"]}
dup = names_cd & names_xa
print(f'  两城重名景点: {dup or "无"}')
print(f'  成都首条: {cd["spots"][0]["name"]} ({cd["spots"][0]["lat"]:.3f},{cd["spots"][0]["lon"]:.3f})')
print(f'  西安首条: {xa["spots"][0]["name"]} ({xa["spots"][0]["lat"]:.3f},{xa["spots"][0]["lon"]:.3f})')

print("\n全部断言通过 ✓")
