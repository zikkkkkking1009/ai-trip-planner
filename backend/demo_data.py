"""演示数据：西安 14 个景点（坐标为 GCJ-02 近似值，仅用于跑通算法）。

之后由「LLM 抽取 + 高德 POI 校正」管线生成，这里手工造数据是为了
让求解器可以零 Key、零成本先跑通。
"""
from models import Spot

XI_AN_SPOTS = [
    Spot(source_id=1, name="秦始皇兵马俑博物馆", lat=34.3847, lon=109.2785,
         stay_min=180, score=9.5, ticket=120, open_h=8.5, close_h=17.0),
    Spot(source_id=2, name="陕西历史博物馆", lat=34.2225, lon=108.9530,
         stay_min=150, score=9.2, ticket=0, open_h=9.0, close_h=17.5),
    Spot(source_id=3, name="西安城墙", lat=34.2760, lon=108.9470,
         stay_min=120, score=8.5, ticket=54, open_h=8.0, close_h=22.0),
    Spot(source_id=4, name="大雁塔", lat=34.2185, lon=108.9640,
         stay_min=90, score=8.3, ticket=50, open_h=8.0, close_h=18.0),
    Spot(source_id=5, name="回民街", lat=34.2650, lon=108.9350,
         stay_min=120, score=8.0, ticket=0, open_h=10.0, close_h=22.0),
    Spot(source_id=6, name="华清宫", lat=34.3620, lon=109.2130,
         stay_min=120, score=7.8, ticket=120, open_h=7.5, close_h=18.0),
    Spot(source_id=7, name="钟楼", lat=34.2610, lon=108.9420,
         stay_min=45, score=7.5, ticket=30, open_h=8.5, close_h=21.0),
    Spot(source_id=8, name="大唐不夜城", lat=34.2150, lon=108.9670,
         stay_min=120, score=8.2, ticket=0, open_h=10.0, close_h=23.0),
    Spot(source_id=9, name="小雁塔", lat=34.2360, lon=108.9400,
         stay_min=75, score=7.2, ticket=0, open_h=9.0, close_h=17.5),
    Spot(source_id=10, name="碑林博物馆", lat=34.2520, lon=108.9470,
         stay_min=90, score=7.4, ticket=50, open_h=8.0, close_h=18.0),
    Spot(source_id=11, name="大明宫国家遗址公园", lat=34.2860, lon=108.9600,
         stay_min=105, score=7.0, ticket=60, open_h=8.5, close_h=19.0),
    Spot(source_id=12, name="永兴坊", lat=34.2690, lon=108.9570,
         stay_min=90, score=7.3, ticket=0, open_h=10.0, close_h=22.0),
    Spot(source_id=13, name="西安博物院", lat=34.2300, lon=108.9320,
         stay_min=90, score=7.1, ticket=0, open_h=9.0, close_h=17.0),
    Spot(source_id=14, name="青龙寺", lat=34.2380, lon=108.9930,
         stay_min=60, score=6.8, ticket=0, open_h=8.5, close_h=17.0),
]
