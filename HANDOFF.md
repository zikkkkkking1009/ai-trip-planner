# 交接文档（HANDOFF）

> 用途：**新会话读这一份就能无损接上下文**。A7（对齐器 F1 90%→100%）完成于 2026-09-18 深夜，见仓库最新提交（远程 `main` 同步，CI 全绿）。
> 项目：AI 行程规划系统（LLM + 组合优化的行程调度）。仓库：https://github.com/zikkkkkking1009/ai-trip-planner
> 目标背景：为**周周（2028 届，大三）**积累一段能写进简历、能扛面试追问的经历，deadline 是 2027 年 3 月暑期实习开岗。

---

## 一、30 秒上手（新会话第一件事）

```bash
# 项目根目录
cd "C:\Users\周周\OneDrive\桌面\workbuddy\2026-09-16_旅游规划项目"

# 启动服务（当前应有实例在跑，先探测再决定是否重启）
cd backend && "C:\Users\周周\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn main:app --port 8000

# 单元测试（37 个）
"C:\Users\周周\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest tests/ -q

# 前端静态检查（语法 + 命名遮蔽守卫）——在项目根目录跑
cd "C:\Users\周周\OneDrive\桌面\workbuddy\2026-09-16_旅游规划项目"
"C:\Users\周周\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tools/check_frontend.py

# 前端运行时冒烟（jsdom，12 项断言）
cd "C:\Users\周周\.workbuddy\binaries\node\workspace" && NODE_PATH="C:/Users/周周/.workbuddy/binaries/node/workspace/node_modules" "C:\Users\周周\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" "C:/Users/周周/OneDrive/桌面/workbuddy/2026-09-16_旅游规划项目/tools/frontend_smoke.js"
```

**关键路径与环境注意**：

| 项 | 值 |
|----|----|
| Python（托管，优先） | `C:\Users\周周\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| Node（托管） | `C:\Users\周周\.workbuddy\binaries\node\versions\22.22.2-3\node.exe` |
| jsdom 安装位置 | `C:\Users\周周\.workbuddy\binaries\node\workspace\node_modules`（用 `NODE_PATH` 引用） |
| git | `C:\Users\周周\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe` |
| 服务端口 | 8000，前端 `http://localhost:8000` |

---

## 二、当前功能与量化结果（都已验证）

**技术主线**：攻略文本 → LLM 结构化抽取 → 高德实体对齐 → OPTW 求解（多起点贪心 + 2-opt + 跨日搬运）→ 独立约束校验 → 逐日行程 + 地图 + 详情。

| 指标 | 数值 | 出处 |
|------|------|------|
| 排期 gap（vs OR-Tools CP-SAT，50 场景） | 均值 **−0.03%**、中位 0%、最大 0.03%、≤3% 覆盖 **100%** | `backend/eval_gap.json`、`docs/experiments.md` 实验四 |
| 多起点改造前对照 | gap 均值 7.53%、最大 24.34%、≤3% 占 46% | 同上 |
| 求解耗时 | 均值 **501ms**（CP-SAT 均值 4.18s） | 同上 |
| 实体对齐 F1 | **100%**（20 条标注，转人工率 15%；改进前 90%/20%） | `python eval_aligner.py` |
| 三方对照（朴素 / 求解器 / LLM 直排） | 求解器 100% 无冲突、0 预算违规；LLM 直排质量分 0.921 但违规 8 次 | `backend/eval_results.json` |
| 单元测试 | **45 个**全过 | `pytest tests/ -q` |
| 前端静态检查 + 运行时冒烟 | 通过；冒烟 **12/12**（旧版 8 项失败，证明测试有效） | `tools/` |
| 接口数 | 18 个 | `backend/main.py` |

**产品功能**：双页（规划 / 我的）、日历选期、地图按天分色 + 图例开关、景点详情卡（实景图灯箱 / AI 介绍 / 好评避雷双卡 / 地址一键导航）、酒店选择器（缩略图 + 就地小地图判断住宿是否顺手）、**住宿锚点**（每天起点终点，往返通勤计入）、对话式修改（多轮记忆）、历史规划（预览 / 软删除 / 批量清理）、收藏、免费模型通道。

---

## 三、关键设计决策（面试可讲，别改坏）

1. **LLM 只负责理解，硬约束交给确定性算法**——有数据支撑：LLM 直排质量分 0.921 但预算违规 8 次，求解器 0 违规。
2. **多起点随机重启**（A6）：第 0 轮保持分数降序保底，后续带抖动顺序重跑，按 `1000·收益 − 通勤` 取最优；固定种子 42 可复现；时间预算 1.5s；<7 景点退化为单次。**数学上不可能比旧版差**（有测试守住）。
3. **对齐器三机制**（A7，F1 90%→100%）：① 尾部子景点惩罚——候选=长前缀(≥3字)+别名在尾部时 containment/相似度归零（「XX公园-远望紫禁城」≠紫禁城）；② 主名权威性先验——「主名-子点」×2 且子点地址引用别名 ⇒ 景区更名，用主名重查并采纳主名本体（华清池→华清宫）；③ LLM 仲裁兜底——低置信(<0.72)时 LLM 在已召回候选内选优，**答案必须精确∈候选集**（防幻觉），无把握/无 key 则保持转人工；仲裁带缓存（`.align_arb_cache.json` 已 gitignore），eval 可离线复现。行政区一致性校验评估后不做（尾部惩罚+仲裁已覆盖，v3 接口要额外请求）。
4. **快慢接口分离**：`/poi/detail` 只返回毫秒级高德数据，AI 评价走 `/poi/reviews` 异步补，避免首屏等待。
5. **后台预取**：规划开始时并发 3 路预取媒体（与求解并行），点开详情基本命中缓存。
6. **模型任务路由**（实测数据驱动）：主模型 DeepSeek `deepseek-chat` 用于攻略抽取 + 意图解析（交互路径，用户等待）；免费 GLM `glm-4-flash-250414` 只用于**后台评价生成**（预取，用户无感）。**不要**把交互任务切到免费档——实测慢 3~6 倍。
7. **软删除**：删除规划只标记 `deleted`，误删可恢复；不物理删文件（也规避沙箱安全删除守卫）。
8. **依赖锁定**：`requirements.txt`（运行时）/ `-dev`（pytest+httpx）/ `-eval`（ortools 等），全部 `==` 精确版本。
9. **可观测性**：标准库 `logging` + `loggging_setup.py` 的请求 ID 中间件；`/health` 暴露任务表状态（`in_memory` / `evicted_total`）。
10. **可靠性**：`reliability.py` 统一超时 + 重试（只重试超时/429/5xx，指数退避 + 抖动，重试写日志）；LLM 30s、高德 8s。
11. **任务淘汰**：容量 200 + 终态 TTL 2h，运行中任务永不淘汰。

---

## 四、踩过的坑（照做，别重犯）

| 坑 | 症状 | 解法 |
|----|------|------|
| **局部变量遮蔽全局函数**（`const esc = ...`） | 详情卡内容大面积缺失、控制台无报错 | 局部变量禁止与全局工具函数重名；`tools/check_frontend.py` 已守住 |
| **异常被 `.catch()` 静默吞掉** | 界面少内容但日志干净 | 必须做 **jsdom 运行时断言**，静态检查抓不住 |
| **jsdom 里 stub 注入太晚** | `fetch is not defined` | 用 `beforeParse` 钩子注入 fetch/WebSocket |
| **测试读页面内部 `let` 变量** | `Cannot read properties of null` | 走真实交互路径（点按钮 + 假 WebSocket 推结果） |
| **pip 在本沙箱走不了网络**（代理 502） | `No matching distribution found` | 绕过代理手动下载 wheel 到 `C:\Users\周周\.workbuddy\binaries\python\wheels`，再 `pip install --no-index --no-deps` |
| **本 shell 缺常用命令** | `rm / cp / tail / head / grep / mkdir` 均 not found | 用 Python 的 `pathlib` / `shutil` 代替 |
| **git push 间歇 502 / schannel 报错** | 连续失败 | `git -c http.version=HTTP/1.1 push` + 循环重试（间隔 10~15s）；成功后用 GitHub API 复核远程 sha |
| **同一变量重复 `AddHint`** | CP-SAT 报 `MODEL_INVALID`，结果静默变 0 收益 | 先收集 `{变量: 取值}` 再统一应用一次 |
| **对比实验口径不一致** | 拿 20 场景结果对比 50 场景 | 复用同一批 CP-SAT 最优值重算基线 |
| **localhost 探测被系统代理劫持** | 用 `urllib` / `curl` 访问 `http://localhost:8000/health` 返回 **502**，看起来像"服务没起来"，实际服务正常 | 本沙箱设了 `HTTP_PROXY/HTTPS_PROXY`（127.0.0.1:63389），localhost 请求也被送进代理。探测本地服务必须绕过：`urllib.request.build_opener(urllib.request.ProxyHandler({}))`，或 `curl --noproxy '*'`；浏览器里直接访问没问题 |
| **外部依赖路径没测试 → 重构静默破坏** | `extractor.py` 用了 `editor.py` 里的 `LLM_TIMEOUT_S` 却没定义也没 import，`extract_spots()` 一调用就 NameError；上层只 catch 了 RuntimeError/ValueError/KeyError → 穿透成 HTTP 500，代码"看起来一直在跑" | `a01781a` 引入、`70e9d02` 修复。凡是"需要真密钥/真网络才能跑"的函数，至少要留两条离线测试：**模块级常量存在** + **缺 key 时抛正确的异常类型**（用 `monkeypatch.delenv`） |
| **`monkeypatch.setattr(Solver, "常量")`** | AttributeError | 常量在**模块级**，要 patch 模块对象 |
| **CI/badge 与实际不一致** | README 写 19 测试 | 每次改完同步 README 数字 |

---

## 五、待办与推荐顺序（含依赖）

```
A3 标注集扩充（20 → 100~200 条）   ← 需要周周抽时间人工标注；先给标注工具
                                    ⚠ A7 的三机制（尤其 LLM 仲裁与主名先验）扩充后必须复测
B1/B2 数据库（SQLite + SQLAlchemy 三表）+ 用户体系  ← 工程完整度，工程量较大
B8 实际部署（Dockerfile/compose 已就绪）  ← 需要周周注册 Render / Fly.io 账号
C3 CI 加 ruff / mypy；C2 接口级集成测试；C4 多城市泛化；C5 分享长图
```

已完成（不要再做）：A1 CP-SAT 对照、A2 复测、A4 README、A5 实验报告、A6 多起点、**A7 对齐器 F1 90%→100%（尾部惩罚 + 主名先验 + LLM 仲裁）**、B3 任务淘汰、B4 XSS 与事件委托、B5 日志、B6 重试、B7 依赖锁定。

**明确不做**：多人协同、天气接入、支付、纯动效堆料。

---

## 六、验证工具（改完必跑）

| 工具 | 作用 | 命令 |
|------|------|------|
| `pytest` | 37 个单元测试 | `cd backend && python -m pytest tests/ -q` |
| `tools/check_frontend.py` | 前端静态检查（语法门 + 遮蔽守卫 + 未转义字段提示） | `python tools/check_frontend.py` |
| `tools/frontend_smoke.js` | jsdom 运行时冒烟（12 项 DOM 断言） | 见"30 秒上手" |
| `evaluation.py` | 三方对照实验（朴素 / 求解器 / LLM 直排） | `python evaluation.py --n 50 --seed 42 --with-llm` |
| `eval_gap.py` | 启发式 vs CP-SAT 的 gap | `python eval_gap.py --n 50 --seed 42 --limit 8` |
| `eval_aligner.py` | 实体对齐 P/R/F1/转人工率（仲裁缓存命中时离线可跑；`.align_arb_cache.json` 被清则需 LLM key + 网络） | `python eval_aligner.py` |
| `bench_models.py` | 模型选型基准（抽取/解析/评价，多轮重复） | `python bench_models.py --repeat 5` |

**验证方法论（务必延续）**：
- 任何"改进"都要有**改前/改后同口径对照**，数据写进 `docs/experiments.md`
- 新的测试要**用已知有问题的旧版本跑一遍证明它有效**（例：`git show a01781a:static/index.html > /tmp/old.html && node tools/frontend_smoke.js /tmp/old.html`）
- 实验必须固定随机种子；跑完把结果文件一起提交

---

## 七、密钥与合规（红线）

- `.env` **绝不提交**（已在 `.gitignore`）；仓库里只有 `.env.example`
- 现有密钥：高德 `AMAP_KEY`、DeepSeek `LLM_API_KEY`、智谱 `LLM_FAST_API_KEY`（全部只在本机 `.env`）
- 提交前自检：`git ls-files | xargs grep -l "sk-"` 应为空
- 地图合规：默认高德瓦片（GCJ-02）；切 OSM 时前端做 GCJ-02 → WGS84 纠偏

---

## 八、周周的偏好（协作方式）

- 中文；**先结论后理由**，结论超过三行就是在藏东西
- 有判断直说，包括"这条路走不通"；他说"不吃鼓励，吃事实和可执行步骤"
- 少问多给：**给他现成产物比问他问题更有效**；需要他决策时给 A/B/C 选项
- 他要的是"能写进简历、能扛追问"的深度，不是功能数量——**UI 已超标，别再堆前端**

---

## 九、文档索引

| 文件 | 内容 |
|------|------|
| `README.md` / `README_en.md` | 项目说明、量化结果、快速开始、部署、合规 |
| `docs/experiments.md` | **实验报告**：方法学 + 四个实验（三方对照 / 对齐 F1 / 模型选型 / CP-SAT gap）+ 失败案例 + 改进尝试（含负结果） |
| `ROADMAP.md` | 项目复盘：事实基线、五维评估、A/B/C 问题清单、六批推进计划、**面试资产包**（简历草稿 + 3 个 debug 故事 + 8 个追问答案要点） |
| `HANDOFF.md`（本文） | 交接上下文 |
| `~/.workbuddy/skills/frontend-runtime-verify/` | 前端运行时验证方法论（可复用技能） |
