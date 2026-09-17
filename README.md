# AI 行程规划系统 v0.5

[![CI](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/tests-19%20passed-brightgreen)

[简体中文](README.md) | [English](README_en.md)

把一段旅行攻略文本变成一份**可校验、可修改、地图可视化**的逐日行程：

```
攻略文本 ──LLM 抽取──▶ 景点别名 ──实体对齐(高德 POI, F1 90%)──▶ 排期求解器(OPTW)
                                                                  │
用户(日期区间/预算/时间窗) ──────────────────────────▶ 约束校验器 ◀┘
                                                                  │
                                          逐日行程 JSON + WebSocket 进度 + 地图路线
```

核心设计：**LLM 只负责理解和抽取，所有硬约束（预算/时间窗/开放时间）交给确定性算法**。
这个决策不是拍脑袋——50 组场景的三方评估显示，纯 LLM 排期有 18% 的概率超预算，
而本方案的求解器为 0（见[评估实验](#评估实验)）。

## 功能特性

- **排期求解器**：将行程规划建模为带时间窗的定向问题（OPTW），贪心构造 + 天内 2-opt + 跨日搬运，预算在构造阶段即生效，装不下的景点给出原因（预算不足/时间窗冲突）
- **地理实体对齐**：攻略别名（「兵马俑」「紫禁城」）→ 高德标准 POI。召回 Top-5 → 四路打分融合（包含关系/字符相似/类型先验/后缀扩展）→ 干扰类型硬过滤（公交站/行政区不参选）→ 低置信度转人工。20 条标注集上 F1 从 80% 迭代至 90%
- **通勤矩阵**：高德距离测量 API 真实驾车时长 + 三级缓存（进程内 memo → 文件 → API）+ 限频节流（个人 Key 3QPS）+ 无 Key 自动降级估算。实测重复求解 API 调用量降为 0
- **异步任务系统**：`/plan/async` 秒回 task_id，后台协程执行完整流水线，WebSocket 实时推送阶段进度，轮询接口兜底——解决长耗时请求被网关 504 的问题
- **约束校验器**：预算超支（硬违规）/ 日负载过重（软）/ 通勤占比 > 40%（软，提示绕路）/ 空天
- **路书前端**：手机卡片式 UI、日期区间选择、Day 分页 tab、Leaflet 地图按天分色路线 + 图例开关，底图可配置（高德 / OSM，含 GCJ-02↔WGS84 转换）
- **LLM 抽取**：OpenAI 兼容接口（DeepSeek/Qwen 均可）+ JSON 容错解析

## 快速开始（30 秒，零 API Key）

```bash
git clone https://github.com/zikkkkkking1009/ai-trip-planner.git
cd ai-trip-planner/backend
pip install pydantic
python run_demo.py
```

启动完整服务（含 Web 界面）：

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# 打开 http://localhost:8000 → 点「开始规划」→ 切「🗺 地图」看路线
```

配置 Key（可选，解锁真实通勤数据与 LLM 抽取）：

```bash
cp .env.example .env   # 填入 AMAP_KEY / LLM_API_KEY
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 路书界面 |
| GET | `/health` | 健康检查 |
| GET | `/demo/spots` · `/demo/config` | 演示数据 / 底图配置 |
| POST | `/plan` | 同步排期（简单场景） |
| POST | `/plan/async` | 异步排期，返回 task_id |
| GET | `/task/{id}` | 任务状态轮询 |
| WS | `/ws/{id}` | 实时进度推送 |
| POST | `/extract` | 攻略文本 → LLM 抽取 → 实体对齐 |

## 评估实验

50 组场景（seed=42 可复现），质量函数 = 0.4·(1-通勤占比) + 0.4·(1-时间冲突率) + 0.2·(1-负载不均衡)：

| 指标 | 朴素装填基线 | 真 LLM 排期 (deepseek-chat) | OPTW 求解器 |
|------|-------------|---------------------------|------------|
| 平均时间冲突数/行程 | 0.38 | 0.06 | **0** |
| 无冲突行程占比 | 62% | 94% | **100%** |
| 通勤占比 | 23.5% | 13.0% | **13.1%** |
| **预算违规** | 12/50 | **9/50 (18%)** | **0** |
| 单次排期耗时 | <1ms | ~800ms + API 费用 | **~1ms，零成本** |

结论：给足结构化数据后 LLM 排期质量不差，但**预算约束控制不住**（18% 场景超支），且有 API 成本与延迟。求解器保证全部硬约束且零成本——评估数据支撑「LLM 做理解与抽取，约束交给确定性算法」的架构决策。详见 `backend/eval_results.json`。

## 目录

```
backend/
├── models.py            # 数据模型（Spot / PlanRequest / DayPlan / UnplannedSpot / PlanResult）
├── solver.py            # 排期求解器：贪心构造（预算硬约束）+ 2-opt + 跨日搬运，通勤函数可注入
├── commute.py           # 通勤矩阵：高德 /v3/distance + 三级缓存 + 限频节流 + 无Key降级
├── aligner.py           # 实体对齐：别名→高德POI（召回→打分融合→类型硬过滤→阈值分流）
├── eval_aligner.py      # 对齐评估：标注集 + P/R/F1 + 转人工率
├── evaluation.py        # 三方评估实验（朴素基线 / 真LLM / 求解器）
├── tasks.py             # 异步任务系统：TaskManager + 后台流水线 + 进度推送
├── constraint_check.py  # 约束校验器
├── extractor.py         # LLM 抽取（OpenAI 兼容 + JSON 容错解析）
├── main.py              # FastAPI 入口
├── tests/               # 19 个单元测试（求解器/校验器/对齐打分/异步任务）
└── ...
static/index.html        # 路书前端（原生 JS + Leaflet，无构建步骤）
```

## 路线图

- [ ] 对话式修改行程（function calling：「明天下午加个附近的咖啡馆」）
- [ ] 行程长图导出 / 分享页
- [ ] OR-Tools CP-SAT 最优解对比实验（量化启发式 gap）
- [ ] 任务持久化（磁盘/Redis）；标注集扩充至 200 条；LLM 别名规范化前置

## 合规与密钥管理

- **密钥**：全部通过 `backend/.env` 配置（已被 .gitignore 排除），仓库全部提交历史经特征检索确认无泄露。Key 泄露时在对应控制台重置即可，无需改代码
- **地图底图**：默认高德栅格瓦片仅建议本地开发/演示使用；生产环境请使用官方 JS API（Web端 Key），或在 `.env` 设 `TILE_PROVIDER=osm`（前端自动做 GCJ-02→WGS84 转换）
- **数据**：演示数据为手工整理的公开信息；未采用任何平台内容抓取（以「粘贴攻略正文」为主输入路径）
- **许可证**：MIT

## 致谢

架构设计参考了 [liketrek/TREK](https://github.com/liketrek/TREK)、[1sdv/TripStar](https://github.com/1sdv/TripStar)、[OSU-NLP-Group/TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner) 等开源项目的思想（仅借鉴设计，未复制代码）。

## 更新日志

- **v0.5** 异步任务系统（task_id + WebSocket 进度推送）、路书式前端（日期区间 + Day tab + 地图路线）、底图配置化
- **v0.4** 工程化：单元测试 + GitHub Actions CI + MIT License
- **v0.3** 三方评估实验（朴素基线 / 真 LLM / 求解器，50 组场景）+ 地理实体对齐管线（F1 80%→90%）
- **v0.2** 高德通勤矩阵（三级缓存 + 限频节流）、预算改为构造期硬约束
- **v0.1** OPTW 启发式求解器 + 约束校验器 + LLM 抽取模块
