# AI 行程规划系统

[![CI](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Tests](https://img.shields.io/badge/tests-34%20passed-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

[简体中文](README.md) | [English](README_en.md)

把一段旅行攻略文本，变成一份**约束可校验、可对话修改、地图可视**的逐日行程。

```
攻略文本 ──LLM 抽取──▶ 景点候选 ──实体对齐(高德 POI)──▶ 可求解实例
                                                          │
用户(日期/预算/时间窗/住宿) ────────────────▶ OPTW 求解器(贪心+2-opt+跨日搬运)
                                                          │
                                              约束校验器(预算/时间窗/通勤占比)
                                                          │
                                    逐日行程 JSON + WebSocket 进度 + 路书前端
```

**核心设计决策：LLM 只负责「理解」，所有硬约束交给确定性算法。**
这不是拍脑袋——50 组场景的三方对照实验显示，让 LLM 直接排期，行程质量分甚至略高（0.921 vs 0.915），
但**有 8 次预算违规、2% 的场景存在时间冲突**；本项目求解器为 **100% 合规、0 违规**。
数据见 [docs/experiments.md](docs/experiments.md)。

---

## 量化结果

**排期方案三方对比**（50 组程序生成场景，固定随机种子 42，详见实验报告）

| 指标 | 朴素装填基线 | **本项目求解器** | LLM 直接排期 |
|------|-------------|----------------|-------------|
| 每计划时间冲突数 | 0.38 | **0** | 0.02 |
| 无冲突率 | 62% | **100%** | 98% |
| 预算违规次数 | 12 | **0** | 8 |
| 通勤时间占比 | 23.5% | **13.1%** | 13.1% |
| 行程质量分 | 0.857 | 0.915 | 0.921 |

**实体对齐**：20 条标注集上 Precision 90% / Recall 90% / F1 90%，低置信度转人工率 20%（用户点选兜底）。

**最优解对照**（与 OR-Tools CP-SAT 同模型对照，50 场景）：gap 均值 **7.57%**、中位 5.9%、≤3% 占 46%。
启发式耗时 **1 毫秒**（CP-SAT 均值 4.18 秒）；宽松实例（≤10 景点）两者均为最优（gap 0%），
紧张实例（13 景点 / 2 天）差 21~24% —— 瓶颈在「景点选择」而非「排序」。详见实验报告。

**模型选型**（每任务重复 5 次，详见实验报告）：免费模型在**用户等待路径**上慢 3~6 倍
（抽取 12.12s vs 1.88s、意图解析 2.56s vs 0.78s），因此交互任务保留主模型；
**后台评价生成**改用免费模型（5.74s vs 1.98s，但发生在预取阶段，用户无感）——做到「交互不降速 + 生成零成本」。

---

## 功能特性

**规划链路（后端）**

- **OPTW 求解器**：行程规划建模为带时间窗的定向问题，贪心构造 + 天内 2-opt + 跨日搬运；预算在构造阶段生效，装不下的景点给出原因（预算不足 / 时间窗装不下）
- **住宿锚点**：酒店是每天的起点与终点——往返通勤计入时间窗与统计，而不是作为某个景点插入某天
- **地理实体对齐**：攻略别名（「兵马俑」「紫禁城」）→ 高德标准 POI：召回 Top-5 → 四路打分融合（包含关系/字符相似/类型先验/后缀扩展）→ 干扰类型硬过滤 → 低置信度转人工
- **通勤矩阵**：高德驾车时长 + 三级缓存（进程内 → 磁盘 → API）+ 限频节流；无 Key 自动降级为直线估算
- **约束校验器**：独立复核求解结果（预算 / 时间窗 / 通勤占比 / 空天），不信任求解器自述
- **异步任务 + 实时进度**：`/plan/async` 秒回任务 ID，后台协程执行，WebSocket 推阶段进度，轮询兜底
- **对话式修改**：LLM 解析意图（remove/add/replace/pin_add/hotel）→ 确定性代码执行 → 重排 + 复校验；支持**多轮指代**（先说「酒店是汉庭」，再说「换到西安站附近」）
- **景点媒体与 AI 评价**：高德图库实景图 + 营业时间 + 地址；评价摘要由 LLM 生成并缓存，规划完成后**后台并发预取**，点开即显

**路书前端（`static/index.html`，零构建）**

- 双页结构：**规划**（表单 → 进度日志 → Day 分页行程 → 地图）与**我的**（历史规划 + 收藏景点）
- 日历区间选择（先出发后结束、区间高亮、点已选日期取消）
- Leaflet 地图按天分色路线、图例开关、住宿锚点标记；底图可切换高德 / OSM（含 GCJ-02↔WGS84 转换）
- 景点详情卡：实景图灯箱、AI 介绍、好评 / 避雷双卡、地址一键唤起高德导航
- 酒店选择器：检索 → 缩略图 → 就地展开小地图（橙点=酒店、彩点=当前行程景点）判断住宿是否顺手
- 历史规划预览 / 软删除 / 批量清理；收藏与行程联动

---

## 快速开始

**零 API Key 跑通**（走内置演示数据 + 直线距离估算）：

```bash
git clone https://github.com/zikkkkkking1009/ai-trip-planner.git
cd ai-trip-planner/backend
pip install pydantic
python run_demo.py
```

**启动完整服务**：

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8000
# 打开 http://localhost:8000 → 点「开始规划」→ 切「🗺 地图」看路线
```

**配置 Key（可选，解锁真实通勤、LLM 抽取与 AI 评价）**：

```bash
cp backend/.env.example backend/.env
```

| 变量 | 用途 | 说明 |
|------|------|------|
| `AMAP_KEY` | 高德 Web 服务 Key | 通勤时长、POI 检索、实景图 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID` | 主模型 | 攻略抽取等重任务（OpenAI 兼容即可） |
| `LLM_FAST_API_KEY` / `LLM_FAST_BASE_URL` / `LLM_FAST_MODEL` | 轻任务快模型 | 意图解析、评价生成；**可指向免费模型，不配则回落主模型** |
| `TILE_PROVIDER` | 底图 | 默认高德；`osm` 切 OpenStreetMap |

> 实测可用的免费轻任务模型：智谱 `glm-4-flash-250414`（0.5~0.8s 单轮；注意 `glm-4.5-flash` 与 `glm-4.7-flash` 实测 22s / 过载，不要用）。

---

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 路书前端 |
| GET | `/health` | 健康检查（含求解器与高德状态） |
| GET | `/demo/spots` · `/demo/config` | 演示景点（含图片与介绍）/ 底图配置 |
| POST | `/extract` | 攻略文本 → LLM 抽取 → 实体对齐 |
| POST | `/plan` · `/plan/async` | 同步 / 异步排期 |
| GET | `/task/{id}` · WS `/ws/{id}` | 任务状态（轮询 / 实时进度） |
| POST | `/plan/edit` | 对话式修改行程（多轮记忆） |
| POST | `/hotel/search` · `/hotel/set` | 酒店检索 / 设为住宿锚点并重排 |
| GET | `/poi/detail` · `/poi/reviews` | 地点详情（快接口）/ AI 评价（可异步补） |
| GET | `/plans` | 历史规划列表 |
| DELETE | `/plans/{id}` · POST `/plans/delete` | 删除 / 批量删除（软删除，可恢复） |
| GET | `/favorites` · POST `/favorites` | 收藏列表 / 增删 |

---

## 工程实践

- **测试与 CI**：29 个单元测试（对齐 / 求解 / 校验 / 编辑器 / 任务流水线），GitHub Actions 全绿
- **缓存与限频**：通勤矩阵三级缓存（重复求解 API 调用降为 0）；高德 0.35s 节流
- **快慢接口分离**：详情接口只返回毫秒级可达的高德数据，AI 评价走二次请求异步补，避免首屏等待
- **后台预取**：规划开始时即并发预取媒体（与求解并行），用户点开详情时通常已命中缓存
- **软删除**：删除历史规划只标记 `deleted`，误删可恢复，且不做批量物理删除
- **可观测性**：标准库 `logging` + 请求 ID 中间件，一条链路可串起所有日志；`LOG_LEVEL` / `LOG_FILE` 可控
- **可靠性**：统一超时与重试（`reliability.py`）——只重试超时/429/5xx，指数退避 + 抖动，重试事件写日志
- **内存保护**：任务表容量 200 + 终态任务 TTL 2 小时惰性清理，运行中的任务永不淘汰（`/health` 可观测）
- **依赖锁定**：运行时 / 开发 / 实验三套 requirements，全部精确版本，部署可复现
- **降级策略**：无高德 Key → 直线估算；无 LLM Key → 评价区块自动隐藏；模型不可用 → 回落主模型

---

## 部署

```bash
# 方式一：Docker（推荐）
docker build -t ai-trip-planner .
docker run -p 8000:8000 --env-file backend/.env -v $(pwd)/data:/app/data ai-trip-planner

# 方式二：docker compose（含健康检查与数据卷）
docker compose up -d
```

要点：**密钥不进镜像**（`.dockerignore` 排除 `.env`，运行时注入）；`data/` 挂卷持久化规划快照与收藏；
单 worker 即可（求解是毫秒级 CPU 小任务，需要更强吞吐时横向扩实例）。

部署到托管平台（Render / Fly.io 等）时：设置环境变量 `AMAP_KEY` / `LLM_*`，
并把持久磁盘挂到 `/app/data`。**目前尚未实际部署上线，公网链接待补**。

---

## 评估与实验

```bash
cd backend
python evaluation.py --n 50 --seed 42 --with-llm   # 排期：朴素基线 vs 求解器 vs LLM 直排
python eval_aligner.py                             # 实体对齐：P / R / F1 / 转人工率
python bench_models.py --repeat 3                  # 模型选型：抽取 / 意图解析 / 评价生成
python eval_gap.py --n 50 --seed 42 --limit 8      # 启发式 vs CP-SAT 最优：gap（需 requirements-eval.txt）
```

实验结果、方法学与失败案例分析见 **[docs/experiments.md](docs/experiments.md)**。

---

## 目录结构

```
backend/
  main.py                 FastAPI 入口与 18 个接口
  models.py               Pydantic 领域模型（Spot / PlanRequest / DayPlan / Hotel）
  extractor.py            攻略文本 → 景点候选（LLM + 容错 JSON 解析）
  aligner.py              实体对齐（高德 POI 检索 + 打分融合 + 类型过滤）
  solver.py               OPTW 求解器（贪心 + 2-opt + 跨日搬运）
  solver_cpsat.py         OR-Tools CP-SAT 精确解（对照实验 / 可选出更优解）
  reliability.py          统一超时与重试（LLM + 高德共用）
  logging_setup.py        日志配置与请求 ID 上下文
  constraint_check.py     独立约束校验
  commute.py              通勤矩阵（高德 API + 三级缓存 + 限频）
  editor.py               对话式编辑（LLM 意图解析 + 确定性执行 + 评价生成）
  tasks.py                异步任务与进度推送、媒体预取
  demo_data.py            西安演示数据
  tests/                  34 个单元测试（含 CP-SAT 与任务淘汰）
static/index.html         路书前端（零构建）
data/plans/               规划快照（软删除标记）
docs/experiments.md       实验报告
ROADMAP.md                项目复盘与推进路线图
```

---

## 已知局限

- 演示数据仅西安，多城市泛化未验证（`city` 已参数化，替换数据即可）
- 持久层为 JSON 文件，无数据库与用户体系（多用户隔离待补）
- 任务对象在内存中无淘汰策略；无结构化日志
- 无最优解对照（没有 CP-SAT / 下界，因此无法给出 gap）

以上均已在 **[ROADMAP.md](ROADMAP.md)** 中列出解决方案、优先级与依赖关系。

---

## 合规与密钥管理

- **不提交任何密钥**：`.env`、缓存文件、运行数据均在 `.gitignore` 中；仓库内只有 `.env.example`
- **地图合规**：默认底图为高德瓦片（GCJ-02）；切到 OSM 时前端用 eviltransform 算法做 GCJ-02 → WGS84 纠偏
- 演示数据中的景点信息、图片与地址来自高德开放平台公开接口，仅用于技术演示
- 密钥泄露风险自检：`git ls-files | xargs grep -l "sk-"`（应为空）

---

## 更新日志

- **v1.0（2026-09-18）**：CP-SAT 精确解对照实验（gap 量化 + 下界约束保证不差于启发式）、日志与请求 ID、统一重试与超时、任务淘汰与内存保护、依赖锁定、前端 XSS 转义、Docker 部署文件、测试增至 34 个
- **v0.9（2026-09-18）**：双页结构（规划 / 我的）、住宿锚点、景点详情卡与 AI 评价、图片灯箱、地图导航、历史规划软删除与批量清理、对话多轮记忆、快慢接口分离与后台预取、轻任务免费模型通道、评测脚本固定随机种子
- v0.5：对话式修改、异步任务与 WebSocket 进度、地图分色路线、实体对齐评估
- v0.3：OPTW 求解器 + 约束校验 + 通勤矩阵缓存
- v0.1：LLM 抽取 + 实体对齐原型
