# 交接文档（HANDOFF）

> 用途：**新会话读这一份就能无损接上下文**。
> ✅ **2026-09-24 已同步**：**SSH 已配好**（`~/.ssh/id_ed25519`，remote 已切 `git@github.com:zikkkkkking1009/ai-trip-planner.git`），58 个提交已推上远程 `main`（`f79fa7f..5f86b91`）。以后 `git push` 免交互。
> ② 本项目**已迁移到 `D:\workby room\ai-trip-planner`**（旧文档里的 `C:\Users\周周\OneDrive\桌面\...` 已失效）；③ 单元测试 **84 → 349**、接口 **20 → 26**、前端冒烟 **12 → 22+48**（详见二、六节）。
> 项目：AI 行程规划系统（LLM + 组合优化的行程调度）。仓库：https://github.com/zikkkkkking1009/ai-trip-planner
> 目标背景：为**周周（2028 届，大三）**积累一段能写进简历、能扛面试追问的经历，deadline 是 2027 年 3 月暑期实习开岗。

---

## 一、30 秒上手（新会话第一件事）

```bash
# 项目根目录
cd "D:\workby room\ai-trip-planner"

# 启动服务（当前应有实例在跑，先探测再决定是否重启）
cd backend && "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000

# 单元测试（349 个）
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest backend/tests/ -q

# 前端静态检查（共 10 项：语法 / 遮蔽 / 硬编码坐标 / 属性转义 / CSS 自引用 / 未定义变量 / 文档完整性 + 对比度达标 + 无容器级硬编码颜色）——在项目根目录跑
cd "D:\workby room\ai-trip-planner"
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tools/check_frontend.py

# 对比度双门禁：页面级（读真实 :root）+ 浏览器运行时实测（真渲染）
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tools/check_contrast.py
node tools/contrast_runtime.js static/index.html static/home.html --all

# 前端运行时冒烟（jsdom，22 项断言；另有 hotel_smoke.js 48 项，见第六节）
cd "D:\workby room\ai-trip-planner" && node tools/frontend_smoke.js
cd "D:\workby room\ai-trip-planner" && node tools/hotel_smoke.js
```

**关键路径与环境注意**：

| 项 | 值 |
|----|----|
| Python（托管，优先） | `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（**裸 `python` 没有 pytest，必须用这个绝对路径**） |
| Node（托管） | `C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-5\node.exe`（**不在 PATH**，2026-10-02 实测须用全路径） |
| jsdom 安装位置 | `C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules`（`tools/` 下亦可解析） |
| git | `E:\安装软件\Git\cmd\git.exe`（`git version 2.55.0.windows.5`，能连通 GitHub） |
| 服务端口 | 8000，前端 `http://127.0.0.1:8000/app`（`/` 是首页） |
| ⚠️ 多会话共存 | 本仓库可能同时有**多个会话**在改（例：2026-09-24 有另一会话在做设计令牌）。**提交时必须显式 `git add <路径>`，禁止 `git add -A`**，否则会把别人的在途改动一起提交。 |

---

## 二、当前功能与量化结果（都已验证）

**技术主线**：攻略文本 → LLM 结构化抽取（**含城市识别**）→ 高德实体对齐 → OPTW 求解（多起点贪心 + 2-opt + 跨日搬运）→ 独立约束校验 → 逐日行程 + 地图 + 详情。**城市参数贯穿全链路**（`cities.py` 城市中心表 + 前端选择器），支持 5 个预置城市与任意城市（粘攻略→识别城市→对齐）。

| 指标 | 数值 | 出处 |
|------|------|------|
| 排期 gap（vs OR-Tools CP-SAT，50 场景） | 均值 **−0.03%**、中位 0%、最大 0.03%、≤3% 覆盖 **100%** | `backend/eval_gap.json`、`docs/experiments.md` 实验四 |
| 多起点改造前对照 | gap 均值 7.53%、最大 24.34%、≤3% 占 46% | 同上 |
| 求解耗时 | 均值 **501ms**（CP-SAT 均值 4.18s） | 同上 |
| 实体对齐 F1 | **100%**（20 条标注，转人工率 15%；改进前 90%/20%） | `python eval_aligner.py` |
| 三方对照（朴素 / 求解器 / LLM 直排） | 求解器 100% 无冲突、0 预算违规；LLM 直排质量分 0.921 但违规 8 次 | `backend/eval_results.json` |
| 单元测试 | **349 个**全过（另 1 skipped：无 AMAP_KEY 时跳过的酒店用例） | `pytest backend/tests/ -q` |
| 前端静态检查 + 运行时冒烟 | 静态 **7 项**全绿；冒烟 **14/14**（行程渲染）+ **30/30**（酒店弹层 / 长图预览 / 地图视图 / 首屏空状态） | `tools/` |
| 接口数 | **26 个**（13 GET / 11 POST / 1 DELETE / 1 WS；含新增 `/hotel/recommend` 与 `/meta`） | `backend/main.py` |

**产品功能**：**桌面网站形态**（顶部毛玻璃导航 + Hero + 左表单/右结果双栏，<1024px 自动降级单列；已去掉 460px 手机壳）、**桌宠式 AI 对话助手**（右下角常驻角色：眨眼/呼吸/光标跟随/四表情，点击展开气泡面板，消息气泡 + 动态快捷指令 + 思考动画；能改行程**也能回答行程问题**；对话操控走主模型，消息用 textContent 防 XSS）、**偏好选择**（均衡 / 少走路 / 省钱 / 多玩，权重经扫描标定）、**稳健性模拟**（1000 次抽样估"按时走完的概率"+ 给出"去掉哪个景点能提升多少"，一键执行；参数敏感性见实验七）、**多城市**（**25 城 254 个景点**预置 + 任意城市走「粘攻略→识别城市→对齐」，`build_demo_data.py --auto` 可继续扩容）、**粘贴攻略自动识别景点**（LLM 抽取 + 城市识别 + 低置信度条目自动跳过）、日历选期、地图按天分色 + 图例开关、景点详情卡（实景图灯箱 / AI 介绍 / 好评避雷双卡 / 地址一键导航）、**酒店选择器（2026-09-24 重做：打开即推荐附近酒店，无需先搜索；关键词收窄；分页「加载更多」每页 25 条；卡片给出评分 / 归一化档位 / 区域 / 到行程中心的真实距离 / 地址 / 电话 / 多图画廊；三种排序 + 档位·评分筛选；一键更换；就地小地图；⚠️ 没有真实房价——高德免费接口不含房价，字段留空即不显示、绝不编造）**、**住宿锚点**（每天起点终点，往返通勤计入；支持「先选酒店再规划」，`PlanRequest.hotel` 透传并回显）、对话式修改（多轮记忆）、历史规划（预览 / 软删除 / 批量清理）、收藏、免费模型通道。

**界面（2026-09-24 统一）**：全站（左栏表单 / 右栏结果 / 酒店弹层 / 我的页 / 日历 / 长图）统一到 **OpenDesign `modern-minimal`** 方向（发丝描边、除浮层外不用阴影、展示字号紧字距、数字等宽、字重收敛）；**全站图标由 emoji 换为线性 SVG**（16 视窗 / currentColor / 1.8px 描边；酒店图标用 Lucide `hotel`，保留桌宠 🐋）；**地图每日配色为 7 个不同色相**（明度压到 53~54% 以保证色块上白字 ≥4.5:1，算法与断言见 `docs/design/contrast-audit.py`）；**分享长图改为先弹预览再「下载 / 取消」**；**用户面文案去黑话**（hero、说明 chip、识别提示、规划进度条阶段名改白话，折叠日志仍留原始阶段）。

**令牌层与暗色主题（2026-09-24 批 2/3/4 落地，详见 `docs/design/`）**：字号 7 档 / 字重 4 档（**800 全并入 700**）/ 间距 8pt 网格 / 圆角 4 档；`--accent` 拆成「作底 `--accent-solid`」与「作正文 `--accent-deep`」，压在其上的字用成对的 `--on-accent`；**新增暗色主题**：默认**跟随系统**（`matchMedia`），导航栏提供**手动切换**（自动 → 亮 → 暗，选择存 `localStorage.theme`）。实现要点：暗色令牌只有一份（`:root[data-theme="dark"]`），由两页 `<head>` 的脚本按「localStorage → 系统偏好」写 `data-theme`，首帧前落地无闪烁；因为脚本读的是 `matchMedia`，**`tools/contrast_runtime.js` 模拟系统偏好时仍能验到暗色**。独立复核：浏览器运行时实测明暗两套**全部达标**（规划页 36 个文字元素、首页 216 个）。

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
12. **多城市泛化**（N1，2026-09-19）：城市中心表 `cities.py`（坐标由 `tools/build_demo_data.py` 从高德地理编码抓取，带 adcode 可复核）+ 前端城市选择器（datalist，支持自定义输入）+ LLM 抽取识别城市（自动切换）。**未知城市必须显式提示，绝不静默回落到默认城市**——旧版把抽取兜底坐标写死成西安，粘成都攻略会得到「景点名是成都、坐标全在西安」且**不报错**的行程（静默错误比报错危险）。演示数据 5 城 54 个景点，坐标全部来自高德真实抓取（`demo_spots.json`），门票/停留为演示近似值（文件头已标注）。
13. **置信度归一**：对齐打分是加权和（权重合计 1.15），内部阈值判断仍用加权分（`AUTO_THRESHOLD=0.72` 在该尺度上调参），对外输出经 `_confidence()` 截断到 [0,1]——否则会出现「置信度 1.02」这种不合理值。

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
| **bash 内联脚本里的反引号被 shell 吃掉** | 用 `python -c "...含 \`反引号\` 的字符串..."` 批量改文档时，反引号被 shell 当作命令替换执行（报 `xxx: command not found`），写进文件的文本因此缺字（README 的 v1.2 条目就丢了两处文件名） | 改文件一律用 Edit/Write 工具；要跑批量脚本就先落成临时 `.py` 文件再执行。**不要把含反引号/`$`的代码塞进 `bash -c` 的字符串里** |
| **jsdom 默认没有 `requestAnimationFrame`** | 用 jsdom 验证「光标跟随 / 动画 / 节流」这类逻辑时会**静默失败**——rAF 未定义导致回调根本不执行，断言看到的是初始值，错误还可能被 virtualConsole 吞掉（实测：小舟朝向翻转怎么测都不生效，查了三轮才发现是环境问题） | 建 JSDOM 时加 `pretendToBeVisual: true`；测试里别用负坐标（会被规范化），需要位置判断时 stub `getBoundingClientRect` |
| **高德 `citylimit=true` 并不严格** | 用 `city=西安` 搜「四川博物院」照样返回成都的地址（友谊西路72号）——城市传错**不一定报错**，更可能静默返回异地 POI | 城市必须由调用方显式传递（前端选择器 / LLM 识别），**不能指望 citylimit 兜底**。`/poi/detail`、`/hotel/search` 都已补 city 参数 |
| ✅ **媒体缓存 key 只有景点名**（已修 2026-09-20） | `spot_media.json` 以景点名为 key → 同名景点跨城市串味（「人民公园」成都/上海都有）；实测「四川博物院」写过一次后，用西安查也会命中成都的数据 | 已修：新增 `backend/media_cache.py` 统一 key 规则（`城市\|景点名`）+ 原子写（临时文件 + `os.replace`，避免并发写坏 JSON）+ 并发写串行化（`asyncio.Lock`）；读取带城市 key 优先、回退裸名（兼容旧数据）；`tools/migrate_media_keys.py` 完成迁移，`fetch_spot_details.py` 升级为全城市预抓取（61 条全有图）；9 个单测锁住「同名跨城市不串味」 |
| **通勤矩阵预计算是 N(N-1) 次调用** | `precompute` 里 `minutes(a,b)` 与 `minutes(b,a)` 因**方向敏感**是两个独立缓存项 → 19 个景点冷启动需 ~342 次高德调用（0.35s 限频 ≈ 120s）；早期注释写「同时缓存正反两个方向」是错的，会误导人以为省一半 | 已修正注释 + 加成本日志。优化方向：与 N2 地理聚类联动（只预算同簇内 + 簇间代表点） |
| **`monkeypatch.setattr(Solver, "常量")`** | AttributeError | 常量在**模块级**，要 patch 模块对象 |
| **CI/badge 与实际不一致** | README 写 19 测试 | 每次改完同步 README 数字 |

---

## 五、待办与推荐顺序（含依赖）

```
N2 地理聚类分天（**实验已完成：负结果**）  ← 50 场景 0 改善/0 变差，USE_GEO_CLUSTER 默认关闭；
                                            复测条件已满足（A2 偏好已上线）→ 用 eval_cluster.py 在 less_walk 下复测
N3 行程稳健性模拟（蒙特卡洛）**✅ 已完成**  ← simulation.py + POST /plan/simulate + 稳健性卡片
                                          （1000 次抽样；风险点用共同随机数对比"去掉它能提升多少"）
                                          参数敏感性见实验七：绝对概率不能当精确值（跨度 48.9pp），
                                          但 Top2 风险点在 5 组参数下全一致 → 主打"建议"而非百分比
N3b 稳健排程 **✅ 已完成**            ← robustness.py：排程用内缩窗口留缓冲、判定用原始窗口；
                                          缓冲量自适应（参考 P90 超时）。实测：7 景点 3.3% → 6 景点 95.3%
                                          （少 1 个景点换 92 个百分点确定性）。前端已加"稳妥度"选择（不要求/70%/85%）
                                          ⚠️ 踩坑记录见实验八："留缓冲"必须缩小可用预算，而不是降低验收标准
**A8 对齐器第二轮：子景点后缀 ✅ 已完成**  ← `aligner._is_appended_subvenue`：与 A7 的尾部结构对称，
                                          要求「主名 + 分隔符 + 短后缀(≤5字)」；两个评分入口都记 0
                                          改前/改后 0/2 → 2/2（改前是 confidence=1.00 的满信心错误）
                                          标注集已补这 2 条难例（22 条 / F1 100%）；详见实验九
**票价「未知 ≠ 免费」✅ 已修**（2026-09-28）  ← 原来未知票价显示成「总门票 ¥0」= 谎称免费。
                                       根因：票价有两个不同的零（已知免费 / 我们不知道）。
                                       高德**不返回票价**（景点 `biz_ext.cost` 实测为空数组）⇒ 只能靠
                                       ①攻略原文 ②本地票价表；两者都没有就必须说"不知道"。
                                       做法：`Spot`/`VisitedSpot` 加 `ticket_known`、`PlanResult.cost_known`；
                                       对齐后用 `demo_data.ticket_of()` **精确名**回填；前端 `moneyTxt()`
                                       统一出口，未知显示「票价待查」。
                                       残留：停留时长仍是演示近似值（可接受，它本就是建议值）
                                       ⚠️ **2026-09-21 实测结论（别再重复试）**：高德 `place/text`（搜索接口）
                                       **不返回 `biz_ext.cost`**——秦始皇兵马俑/故宫/大雁塔等 8 个知名收费景点
                                       覆盖率 **0%**。所以"从高德搜索补票价"这条路走不通。
                                       可行方向：① `place/detail` 详情接口（每景点多 1 次调用，规划阶段成本翻倍）
                                       ② 本地票价表（用预置数据 254 个景点，零 API 成本，覆盖热门）
                                       ③ 只显示"未收录"不假装 0 元（**已被 AI 总评卡片刻意采用**：
                                       总花费标注"门票，不含交通餐饮"，交通单价不可靠就不编）
A3 标注集扩充（20 → 100~200 条）   ← 需要周周抽时间人工标注；先给标注工具
                                    ⚠ A7/N1 的改动（LLM 仲裁、主名先验、城市参数）扩充标注集后必须复测
B1/B2 数据库（SQLite + SQLAlchemy 三表）+ 用户体系  ← 工程完整度，工程量较大
B8 部署（**用户已明确推迟**，理由：功能还不够扎实、泛化刚补完）  ← 免费方案调研见 docs/deploy_freetier.md
**C3 CI 加 ruff / mypy ✅ 已完成**（2026-09-28）  ← 两道硬门禁，**均已清零**：
                                         ruff 21 条（3 处未用 import + 6 处死变量 + commute 一处 import 位置；
                                         main.py 的 13 处 E402 属有意豁免，理由写在 ruff.toml 里）；
                                         mypy 59 条（Optional 收窄 / 补容器标注 / 修 2 处属性重复声明），
                                         **0 处 `# type: ignore`**。配置见 `backend/ruff.toml`、`backend/mypy.ini`
**C2 接口级集成测试 ✅ 已完成**（2026-09-28）  ← `backend/tests/test_api_endpoints.py` 21 条，
                                        覆盖此前零测试引用的 14 个接口；离线 + 落盘重定向 tmp，
                                        含「跨城市数据绝不写缓存」回归（spy 断言 save_media 未被调用）
**C5 分享长图 ✅ 已完成**（2026-09-24）  ← 原来点一下静默下载、样式陈旧；现改为**先弹预览浮层再「下载 / 取消」**，
                                        canvas 也重排为现代简洁版（去蓝色渐变头、细分隔线、字重层级）
设计系统落地（批 1 令牌 / 批 2 可读性 / 批 3 暗色主题 + 界面稿）  ← 完整方案见 `docs/design/ui-design-system.html`
                                  ⚠️ 2026-09-24 由**另一会话**并行推进（产物落 `tools/check_contrast.py`），
                                  对接前先 `git status` 看清谁的文件，**提交只 add 自己的路径**
```

已完成（不要再做）：A1 CP-SAT 对照、A2 复测、A4 README、A5 实验报告、A6 多起点、A7 对齐器三机制、**N1 多城市泛化**、**N2 地理聚类（负结果，已复测证伪）**、**N3 稳健性模拟**、**N3b 稳健排程**、**A8 对齐器子景点后缀**、**A2 偏好权重可调**、B3 任务淘汰、B4 XSS 与事件委托、B5 日志、B6 重试、B7 依赖锁定。

**明确不做**：多人协同、支付。

> **2026-09-23 修订**：原列表中的「天气接入」「纯动效堆料」两项已被重新评估并落地。
> 前者拒绝的三条理由里，「数据源不稳定」被实测证伪（open-meteo 无需注册/无需 key、~0.9s 返回），
> 另两条仍有效 ⇒ 落地时**刻意收紧定位**：天气只做「行程的注脚」（按天、不做独立版块、拿不到就明说）；
> 后者重新界定为「两页共用一套设计令牌 + 只响应用户操作的微交互」，不是堆料。详见 ROADMAP「已修订的判断」。

---

## 六、验证工具（改完必跑）

| 工具 | 作用 | 命令 |
|------|------|------|
| `pytest` | **349** 个单元测试（酒店用例无 `AMAP_KEY` 时跳过） | `python -m pytest backend/tests/ -q` |
| `tools/check_frontend.py` | 前端静态检查 **10 项**（JS 语法 / 全局遮蔽 / 硬编码坐标 / 属性插值转义 / CSS 自引用 / 未定义 CSS 变量 / 文档完整性 / **对比度达标** / **无容器级硬编码颜色**）；设计稿 HTML 也一并检查配色 | `python tools/check_frontend.py` |
| `tools/check_contrast.py` | **页面级对比度门禁**（读 static/ 里真实的 `:root` / 暗色块，按「角色 × 参照底」断言 WCAG） | `python tools/check_contrast.py` |
| `tools/contrast_runtime.js` | **浏览器运行时对比度实测**（真渲染，非推算）；`--all` 跑明暗两套 | `node tools/contrast_runtime.js static/index.html static/home.html --all` |
| `tools/check_backend_health.py` | **五维健壮性审计**（超时 / 重试 / 状态 / 日志 / 成本，静态检查、不需要密钥，已接入 CI） | `python tools/check_backend_health.py` |
| `tools/verify_multicity.py` | **多城市端到端验证**（7 组断言：城市列表 / 各城景点数与坐标落城 / 未知城市不回落 / 成都攻略全链路 / 西安回归 / 同名 POI 不串味）。**需先起服务并把 `BASE` 端口对齐** | `python tools/verify_multicity.py` |
| `tools/build_demo_data.py` | 抓取多城市 demo 景点（高德真实坐标，禁止手写坐标）+ 打印城市中心表 | `python tools/build_demo_data.py` |
| `tools/frontend_smoke.js` | jsdom 运行时冒烟（**22 项**：行程渲染 / 事件委托 / 详情卡 / 转义） | `node tools/frontend_smoke.js` |
| `tools/hotel_smoke.js` | jsdom 运行时冒烟（**30 项**：酒店弹层 推荐·筛选·分页·画廊·先选酒店 + 长图预览 + 地图视图 + 首屏空状态） | `node tools/hotel_smoke.js` |
| `docs/design/contrast-audit.py` | **对比度门禁**（oklch→sRGB 换算 + WCAG 实测；浅/暗两套主题各 27 组断言） | `python docs/design/contrast-audit.py --check` |
| `evaluation.py` | 三方对照实验（朴素 / 求解器 / LLM 直排） | `python evaluation.py --n 50 --seed 42 --with-llm` |
| `eval_gap.py` | 启发式 vs CP-SAT 的 gap | `python eval_gap.py --n 50 --seed 42 --limit 8` |
| `eval_aligner.py` | 实体对齐 P/R/F1/转人工率（仲裁缓存命中时离线可跑；`.align_arb_cache.json` 被清则需 LLM key + 网络） | `python eval_aligner.py` |
| `bench_models.py` | 模型选型基准（抽取/解析/评价，多轮重复） | `python bench_models.py --repeat 5` |

**验证方法论（务必延续）**：
- **每次改完后端，必跑五维审计**：`python tools/check_backend_health.py`（超时 / 重试 / 状态 / 日志 / 成本），CI 已设卡。**这五条是硬底线，别靠记忆去查**：

  | 维度 | 底线要求 | 判罚规则（脚本自动检查） |
  |------|---------|------------------------|
  | **超时** | 所有外部调用（`urlopen` / `OpenAI(...)`）必须显式带 `timeout`，且不得为 `None`/`0` | ❌ 缺 timeout 直接失败 |
  | **重试** | 调外部网络的函数应有 `retry_call`；确实不需要的要在 `RETRY_EXEMPT_FUNCS` 里写明理由 | ⚠️ 未包且未声明 → 警告 |
  | **状态** | 所有 `run_*` 后台任务函数必须 try/except，且失败时把任务置为 `failed`（不能让后台任务静默死掉） | ❌ 缺 try/except 或没置 failed → 失败 |
  | **日志** | 业务模块必须有 logger；**宽泛 `except Exception` 必须留日志或写注释声明理由**（数据类异常与 `raise` 上抛可豁免） | ⚠️ 静默宽泛捕获 → 警告 |
  | **成本** | 限频/缓存/超时常量必须在位（`QPS_MIN_INTERVAL` / `CACHE_TTL_SEC` / `POI_TIMEOUT_S` / `LLM_TIMEOUT_S`）；不得硬编码城市名与坐标 | ❌ 缺常量或硬编码 → 失败 |

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
| `docs/design/ui-design-system.html` | **界面设计规范与高保真稿**：字号 7 档 / 字重 4 档 / 8pt 间距 / 圆角 4 档的令牌表 + 21 组对比度实测 + 3 批落地方案（批 1 令牌 / 批 2 可读性 / 批 3 暗色主题与界面稿） |
| `docs/design/contrast-audit.py` | 对比度门禁脚本（零依赖，可离线跑） |
| `docs/design/solve-tokens.py` | 令牌求解/交叉校验（与两页 `:root` 比对，当前 0 项不一致） |
| `docs/design/home-redesign.html` | 首页改版稿 |
| `HANDOFF.md`（本文） | 交接上下文 |
| `~/.workbuddy/skills/frontend-runtime-verify/` | 前端运行时验证方法论（可复用技能） |

## 附：2026-09-28 外部审查修复记录（ZCode）

全项目代码审查 + 两轮修复，**13 文件 +744/-136**，已并入 `16f52a2`。
完整报告：`docs/reviews/2026-09-28-zcode-review.md`（接口变化也在里面）。

- **High 6 条**：路径穿越（`_plan_snapshot_file` 收口校验 12 位 hex）、详情卡标题存储型 XSS、
  `run_set_hotel` 阻塞事件循环（`to_thread`）、共享 `req_params` 引用被改写（`deepcopy`）、
  `pin_add` 不落盘、图片灯箱整体失效。
- **Medium**：媒体缓存合并写+线程锁（`put_media` → `update_media`）、favorites 读写锁+原子写、
  `MANAGER.spawn()` 托管后台协程（僵尸 running 任务）、`/extract` 改 JSON body + 12000 字上限、
  WS 增量消费（O(n²)→游标）、WS 断线兜底、CSS 笔误 ×5。
- ⚠️ **接口变化**：`/extract` 现在收 `{"text","city"}` JSON body；`media_cache.put_media()` 已改名
  `update_media()` 且语义变为合并写。
- 门禁：pytest **349** / 冒烟 **22+48** / 新增 `check_css_invalid_props`（前端静态现为 10 项阻塞 + 2 项警告）。
- Low 清单（10 项，量级小）与后续建议见报告第四节；**鉴权与多用户**是公开部署前的必做项（报告 §五.4）。

## 附：2026-09-28 部署阶段一（访问令牌 + 限流）

配套计划 `ai-trip 部署计划-花生壳免费档.md`（工作区根，ZCode 制定，定位：朋友娱乐 + 作品集）。
**照做步骤见 `docs/deploy-runbook.md`**（含阶段二~四与三处对原计划的修正）。

- 代码闸门：`verify_token` 罩住 18 个有副作用/花钱的接口；WS 走 `?token=`（失败先 accept 再
  `close(4401)`）；`APP_TOKEN` 留空即无鉴权（本地开发不受影响），但启动告警 + `/meta` 回
  `token_required=false`
- 限流：30 次/分钟/IP，只计花钱路径；`RATE_LIMIT_PER_MIN` 可环境变量覆盖
- **隧道两个坑**：① 花生壳客户端同机 ⇒ `client.host` 恒为 127.0.0.1，按它限流会把所有朋友算成
  同一人；② XFF 取**最后一个**（标准反代是追加式，第一个是攻击者可伪造的）
- 前端：包 `window.fetch` 只对**同源**注入（跨域绝不加）；导航栏令牌面板；401/429/4401 各有提示
- 测试：`backend/tests/test_auth.py` 17 条，含「遍历真实路由表」的门禁型用例
- 门禁：pytest **349** / 前端静态 10 项阻塞 + 2 警告 / 冒烟 **22+48** / ruff、mypy 0
- 待用户操作：注册贝锐 + 实名认证 + 装客户端 + 加隧道（内网主机 `127.0.0.1`、端口 `8000`）
- 注意：`--host` 用 **127.0.0.1**（原计划写 0.0.0.0，没必要且会暴露给局域网）；
  docker-compose 的 `ports` 已收紧为 `127.0.0.1:8000:8000`

## 附：2026-09-28 首页 hero 动效排查（用户反馈「大图变不成动态」）

Zcode 已改 5 次（`8ca1adc` → `5dfd694` → `4be0c8d` → `a7bf812` → `4607f28`）未解决。

- **结论：动画从来没坏。** 真实 Edge 实测：动画在跑、时长正常（`.flow` 5.5s / `.traveler` 7s），
  1.1 秒内有 6 个属性在变；隧道吐的页面与本地逐字节相同（83003 B），排除缓存。
  问题是**幅度**：hero 卡片可见变化像素仅 **2.59%/1.3s**，且多为低对比度的虚线位移
  —— 肉眼读成一张静态插画。
- ⚠️ **本节结论后来被推翻，以文末「21:5x 补记（勘误）」为准**：当时判"用户环境不报 reduce"，
  依据是用**有头** Edge 实测 `reduce=false` —— **那个依据是错的**：Playwright 默认把
  reducedMotion 模拟成 no-preference，"用 Playwright 测 reduce"永远测不到。
- **改法**（`static/home.html`）：主路线改「**自绘**」（底下 `.ghost` 常驻虚线表示"未完成"）
  + 保留整条虚线行进 + 彗星段 12→38、线宽 3.5→5 + 锚点按笔尖顺序依次点亮（0/2.1/4.2s）
  + 整幅插画缓慢推移（`hero-drift`，幅度压在 padding 26px 之内、不溢出白卡）。
- **实测**：逐窗变化率均值 **2.61% → 6.00%**（峰值 6.77%），2.3 倍；各相位 4.6~6.9% 均匀。
- **新增 `tools/hero_motion_check.js`**（**本地**门禁，不进 CI — 需真实浏览器）：覆盖一个周期
  逐窗比像素，断言均值 ≥ 4%；含「冻结全部动画后必须 ≈ 0」的**自校验对照组**
  （证明这把尺子量的是运动而不是噪声）。**已反向验证**：对旧版（2.79%）判红 exit 1。
- **口径勘误（两层错）**：① worker 自报 hotel_smoke `47→49` —— 它的提交确实到 49，但之后
  Zcode 的 `a7bf812`（主题断言二态化）把它降到 **48**，文档一直没跟上；② pytest `348` → **349**。
  README 中英 + HANDOFF 共 20 处数字已全部校正。

## 附：21:5x 补记（勘误）—— hero 动效的最终定案

用户随后甩了一段 6.33s 的录屏（`Delta Force 2026.09.28 - 22.09.42.10.mp4`），一句"哪里动了？？"。
**录屏与"用系统 API 读设置"两条证据合起来，把上一节的结论整条推翻了。**

### 录屏怎么读的（没有 ffmpeg，用浏览器当解码器）
Edge 能解 H.264 ⇒ 起一个**支持 HTTP Range** 的本地服务喂视频，`<video>` 精确 seek + canvas 逐帧导出。
⚠️ 坑：`python -m http.server` **不支持 Range**，Chromium 靠 206 才能 seek ⇒ 每次取到的都是同一帧，
全帧差异算出来 0.00%，差点得出"视频是静止的"错误结论。
**识破办法：看文件体积** —— 9.7 MB / 6.33s ≈ 12 Mbps，纯静止画面 H.264 只会压到几百 KB。

### 录屏给出的结论
- 路线是**整条实线**（无"自绘"未尽段、无彗星、无漂移）⇒ 正是当时那段 reduce 降级渲染出来的静态帧；
- `0.15s` 与 `1.36s` 两帧逐像素 **0.00%** 变化（同一块区域开发机上是 6%/0.8s）⇒ 他那台机器上它是死的。

### 真正的根因（推翻了上一节的判断）
```
SPI_GETCLIENTAREAANIMATION = 0      ← Windows「动画效果」是关的
SPI_GETMENUANIMATION       = 0
```
**Chromium 判定 `prefers-reduced-motion` 读的就是它** ⇒ 站长浏览器**恒报 reduce**。
上一节判"前提不成立"的**依据是错的**：**Playwright 默认把 reducedMotion 模拟成 no-preference**，
所以"用 Playwright 去测 reduce"永远测不到 —— 反过来说，**想测 reduce 必须显式传
`{ reducedMotion: 'reduce' }`；想读系统真值必须读 `SPI_GETCLIENTAREAANIMATION`**（ctypes 可读；
`reg.exe` 被本机安全策略拉黑，别绕）。

### 定案与实现
**hero 插画【有意】不遵守 reduce**（动效是这张卡的产品诉求），手法是给它的 **14 条** animation
声明加 `!important` —— `.hero-card svg .x` 的特异性 0,2,1 高于全局 `*{animation-duration:.001ms!important}`
的 0,0,0，同为 `!important` 时前者胜。其余动效（滚动淡入 / 主题切换 / 桌宠 / 视图过渡）**照旧**尊重偏好。

### 门禁随之升级为双环境（这是这次最该留下的东西）
`tools/hero_motion_check.js` 现在跑两个环境：`no-preference` 与**显式 `reduce`**，都要 ≥4%。
- **反向验证**：对上一版跑 → `reduce` 行判红 `exit 1`，实测 **0.00%**（正常行 5.87%）——**正好复现用户现象**。
- 修好后：正常 5.90% / reduce 5.91%，对照组 0.00%。
- ⚠️ 对照组也得跟着改：hero 加了 `!important` 之后，原来"注入 `*{animation:none!important}`"的
  冻结手法**压不过 hero 的 `!important`**，对照组会假红（6.18%）。改用
  `document.getAnimations().forEach(a => a.pause())` —— 不受选择器特异性影响。

### 两条通用教训
1. **"用 Playwright 验证媒体查询"要当心它自己的默认模拟**，错的不是页面而是尺子；
2. **对照组的实现方式会随被测代码的优先级变化而失效** —— 对照组自己也需要被 review。

## 附：2026-09-28 开启响应 gzip（公网带宽容量 ×3）

起因：用户问「这个网站最多承载多少用户」。实测后发现**瓶颈是带宽而不是算力**，而且**后端根本没开压缩**。

- 实测：`/` 87 KB、`/app` **223 KB**（单文件 SPA，JS/CSS 全内联）；穿隧道 139~185 KB/s
  ⇒ 打开规划页 **1.28 s**；交互很小（一次 `/plan` 响应只有 **2.3 KB**，带宽只在首屏是瓶颈）
- 加 `GZipMiddleware(minimum_size=1024)` ⇒ `/app` **74 KB**、`/` **28 KB**（3.08×/3.15×）⇒ 隧道 **0.69 s**
- 1 GB 月流量可服务的完整会话：约 3000 → **约 9700**
- ⚠️ **必须加在内层**：`add_middleware` 要写在 `@app.middleware("http")` **之前**。放在外层会被
  BaseHTTPMiddleware 吃掉 `content-length` ⇒ `minimum_size` **静默失效**（实测 127 B 的 `/health` 也被压）。
  这是写测试时才发现的 —— 4 条断言里唯独「小响应不压」那条红了。
- 新增接口级断言 4 条（`tests/test_api_endpoints.py`）；pytest 349 → **350**
- 容量结论：**同时在线 3~5 人舒适 / 10 人明显慢 / 20 人接近上限**；
  **真正的日天花板在高德·LLM 平台的配额**（要登录控制台看），不在代码里

## 附：2026-09-28 CI 连挂 10 次的修复（Node 20 vs jsdom 30.x）

现象：`#82`~`#91` 全部失败，且都挂在同一步 `Frontend runtime smoke test (jsdom)`；
本地跑同一条命令却 22/22 通过。**与我们的代码改动无关。**

根因：CI 里 `npm install jsdom`（没钉版本）装到 jsdom 30.x，它带的 undici 用了
Node 22 才有的 `webidl.util.markAsUncloneable`；而 CI 的 `node-version` 是 20
⇒ `require('jsdom')` 直接 `TypeError`，冒烟**连一条断言都没跑到**。开发机是 Node 22，
所以本地一直绿、CI 一路红 —— 这类"只在 CI 复现"的最费时间。

排查过程（记录下来）：
1. GitHub API 取 run 列表 → 定位首个失败 run 与失败步骤
   （⚠️ **job 日志接口要 admin 权限，未登录拿不到，403**；annotations 同理）
2. 本地对齐 CI 的三个差异逐个排除：LF 行尾（干净克隆）、干净 jsdom、**Node 版本**
3. `npx -y node@20 tools/frontend_smoke.js` → **复现同样的 TypeError**
4. 把 jsdom 钉到 30.1.1 再用 Node 20 跑 → **照样崩** ⇒ 光钉版本没用，必须升 Node

修复（`.github/workflows/ci.yml`）：
- `node-version: "20"` → `"22"`（附根因注释与复现命令）
- jsdom 钉到 `30.1.1`：不钉的话上游一发新版就可能让 CI 突然红，且报错是 Node 内部栈、
  完全看不出跟业务代码有关
- 两个 jsdom 冒烟步骤补「失败时把尾巴丢进 `::error::` annotation」（与 pytest 那步同款）
  —— **这次排查最大的障碍就是 Actions 日志要登录才看得到**，以后失败原因直接出现在摘要上

验证：run **#92 = success**，15 个步骤全部 ✓（无 skipped）。提交 `128f52e`。
