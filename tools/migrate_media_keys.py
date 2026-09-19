"""一次性迁移：把 spot_media.json 的 key 从裸景点名改成「城市|景点名」。

为什么要迁移：裸名 key 会让不同城市的同名景点串味（「人民公园」成都/上海都有），
而高德 citylimit 并不严格，串味结果是**静默错误**（不报错，但图片/坐标/地址是别的城市的）。

迁移后：读取仍兼容裸名（`media_cache.get_entry` 会回退），写入统一用带城市的 key。
无法判断城市的条目（不在 demo 数据里，例如手工抓取的临时条目）原样保留并报告。

用法：cd backend && python ../tools/migrate_media_keys.py [--dry-run]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from demo_data import DEMO_SPOTS  # noqa: E402
from media_cache import load_media, media_key, save_media  # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    media = load_media()
    if not media:
        print("媒体缓存为空，无需迁移")
        return 0

    # 景点名 → 可能所属城市（同名跨城市时会有多个）
    name2cities: dict[str, list[str]] = defaultdict(list)
    for city, spots in DEMO_SPOTS.items():
        for s in spots:
            name2cities[s.name].append(city)

    out: dict = {}
    renamed: list[tuple[str, str]] = []
    ambiguous: list[str] = []
    unknown: list[str] = []

    for key, val in media.items():
        if "|" in key:
            out[key] = val
            continue
        cities = name2cities.get(key, [])
        if len(cities) == 1:
            nk = media_key(cities[0], key)
            out[nk] = val
            renamed.append((key, nk))
        elif len(cities) > 1:
            ambiguous.append(f"{key}（{len(cities)} 个城市都有：{cities}）")
            out[key] = val
        else:
            unknown.append(key)
            out[key] = val

    print(f"缓存条目：{len(media)} → {len(out)}")
    print(f"  已按城市重命名：{len(renamed)} 条")
    for old, new in renamed[:5]:
        print(f"    {old} → {new}")
    if len(renamed) > 5:
        print(f"    … 其余 {len(renamed) - 5} 条同理")
    if ambiguous:
        print(f"  ⚠️ 同名跨城市、保留裸名（需人工确认）：{ambiguous}")
    if unknown:
        print(f"  未在 demo 数据中（保留原样）：{unknown[:8]}"
              + (" …" if len(unknown) > 8 else ""))

    if dry:
        print("\n（--dry-run：未写入）")
        return 0
    save_media(out)
    print(f"\n已写入 {Path(__file__).resolve().parent.parent / 'backend' / 'spot_media.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
