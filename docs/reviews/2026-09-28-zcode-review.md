# ai-trip-planner 代码审查与修复报告（2026-09-28）

> 写给 workbuddy：本报告记录 2026-09-28 对 `ai-trip-planner` 做的一次全项目代码审查
> 与两轮修复。所有改动**尚未 commit**，`git status` / `git diff` 可直接查看全部差异；
> 修复后的全量门禁均绿灯（数字见「验证结果」）。接手前建议先读本文件，再看 diff。

## 一、审查方式与范围

- 方式：OCR delegation 模式（open-code-review CLI 取规则 + 三个并行审查代理分批审查 + 逐条人工复核高危发现）；工作树当时是干净的，故按**全项目审查**而非 diff 审查。
- 范围：后端生产代码 16/16 文件、前端 2/2 页面、配置/CI/Dockerfile 全部深审；测试 21 个文件以执行覆盖；跳过 12 个评估/演示脚本（eval_*.py、bench_*、tools/*，不进生产路径）。
- 本地实测：pytest 314 通过、ruff/mypy 清零、前端静态检查与对比度门禁通过、jsdom 冒烟 14/14、酒店冒烟 42/42——静态质量确实高，问题集中在**异步使用、共享状态、转义漏点**。

## 二、改了什么（两轮，共 17 个文件，未 commit）

### 第一轮：6 条 High + 契约/降级

| # | 问题 | 修法 |
|---|------|------|
| 1 | **路径穿越**：`/plans/delete` 等 4 处把无鉴权的 task_id 直接拼进 `PLANS_DIR / f"{tid}.json"`，`../x` 可读写 PLANS_DIR 之外的 .json | main.py 新增 `_plan_snapshot_file()` 收口，校验 `^[0-9a-f]{12}$`（与 uuid4 生成格式一致），4 个入口全部改走它 |
| 2 | **存储型 XSS**：详情卡标题 `${name}` 漏 esc（中文探针抓不住，ASCII 载荷如 `<img src=x onerror=…>` 可注入） | index.html 补 `${esc(name)}` |
| 3 | **事件循环冻结**：`run_set_hotel` 在 async 里直接调带 0.35s 限频 sleep + HTTP 的 `cm.minutes()`，19 景点 = 整个服务卡十几秒 | 预热循环包进 `await asyncio.to_thread()` |
| 4 | **共享引用改写**：`params = base.req_params` 拿引用，hotel/preference 直接写进**基准任务**的入参，两任务共享同一 dict | 改 `copy.deepcopy`；446 行旧的重赋值一并删除（它靠共享引用才把 hotel 传到重排路径） |
| 5 | **pin_add 路径漏收尾**：「加个咖啡馆」主路径成功 return 前不写 chat_history、不落盘 → TTL 淘汰/重启后 404、下轮编辑丢记忆 | 补齐两步，与全局路径同口径 |
| 6 | **图片灯箱整体失效**：`lbUrls` 从未赋值（写进了没人读的 `window.__spotPhotos`）、`paint()` 打开时没调 → 空图、"1 / 0" | 数据源改赋 `lbUrls` + 补 `paint()` 首帧 |

契约与降级：`/plans` 补返回 `cost_known`（历史列表不再把「票价待查」渲染成 ¥N）；前端 `rb.daily_end_h` → `rb.solve_end_h`；高德业务失败（status≠1）在 poi_search/text_search 补 warning 日志（带 info/infocode）；`parse_instruction` 对 `ops` 字段做类型校验（坏类型降级「没听懂」而非任务 failed）；extractor 新增 `_num()` 容错 null/文本、spots 容器与逐条校验、stay_min clamp 进边界——单条坏数据不再报废整份攻略。

### 第二轮：Medium 清零

**并发与数据安全**
- **媒体缓存写锁下沉**：`media_cache` 新增模块级线程锁 + 共享字典 + `update_media()` **合并写**（新键优先、旧键保留）。tasks 预取与 main 的 `/poi/detail`、`/poi/reviews` 统一走它——此前 endpoint 不持锁整份覆盖，会抹掉预取刚写的评价。旧 `put_media()`（无锁）已删除。
- **favorites**：读改写持 `threading.Lock`；文件损坏按空处理并留痕（此前 `GET /favorites` 直接 500）；原子写（tmp+replace）；脏数据缺 name 不再 KeyError。
- **僵尸 running 任务**：新增 `MANAGER.spawn()` 托管后台协程——持强引用防 GC，done 回调把未捕获异常转 failed 并留痕；main.py 三处裸 `asyncio.create_task` 全部替换。此前协程在 `status="running"` 与 `try:` 之间抛错会让任务永远 running、永不淘汰。
- **changes 覆盖**：`run_edit_task` 里 apply_ops 的变更说明改**追加**（旧写法整体覆盖，带 hotel 的编辑丢「住宿设为X」）。

**输入约束**
- `/extract` 改为 **JSON body** + 12000 字上限（超长 413 / 空 400）；`/plan/edit` 指令 2000 字上限。连带修复：旧版用 query 传文本，中文编码后 ×9 字节，长攻略没到校验就先撞网关请求行上限。前端两处调用已同步改 body。

**前端**
- 进度日志 O(n²) 修复：后端 WS 每帧推全量 `progress[]`，两处 WS 改按 `seen` 游标只消费增量。
- WS 断线兜底（两处）：处理 `not_found` + `onclose` 复位按钮/进度条（`finished` 标志区分正常关闭）。
- pickHotel 轮询上限 40 次 ≈28s（后端重启时 /task 持续 404 不再永久锁屏）；showLoading 加「关闭等待」出口、标题补 esc（含外部酒店名）。
- CSS 笔误 ×5：`var(--surface)-space` → `white-space`（导航标签/Day tab/聊天气泡排版恢复）。

**接口变化（workbuddy 接手必读）**
1. `/extract` 现在收 `{"text", "city"}` JSON body，不再收 query 参数。
2. `media_cache.put_media()` 已改名 `update_media(city, name, entry)`，语义变为**合并写**。
3. 3 个旧测试的假 task_id（"t1"/"a"）被改成 12 位 hex——它们被新的格式校验正确拦下，测试意图未变。
4. main.py 不再导入 `load_media/save_media/media_key`（demo_spot_list 改用 `get_media`）。

## 三、验证结果

- pytest **331 passed, 1 skipped**（原 314 + 净增 17 条回归测试）
- ruff / mypy（51 文件）全部清零
- tools/check_frontend.py ✅（新增 `check_css_invalid_props` 门禁）、五维健壮性审计 0 错 0 警、对比度门禁 ✅
- jsdom 冒烟 **18/18**、酒店冒烟 **47/47**、run_demo.py 正常
- 关键新断言做了**反向验证**：拿修复前的旧版页面跑，恰好只挂新断言（灯箱首帧、ASCII 载荷转义、日志去重「出现 2 次」）；旧 CSS 被新门禁检出 5 处、新版 0 处。

新增测试注意点：测试里显式掐掉 AMAP_KEY 与磁盘缓存（conftest 只掐了 selftest，本机 .env 有真 Key，不掐会真发请求真花钱）。

## 四、还需要改什么（均为 Low，量级小）

1. **酒店搜索无请求序号/AbortController**：慢的旧响应可能后到覆盖新列表（关键词与列表不一致）。
2. **review/sim 标记失败不回滚**：`reviewForTask/simForTask` 在 fetch 成功前置位，一次网络失败后该任务永不重试。
3. **缩略图不回写 `spots[]`**：每次 renderItin 重新打一遍 `/poi/detail`（无客户端缓存，虽限并发）。
4. **`task.error` 原样外抛**：异常字符串经无鉴权的 `/task` 返回，个别异常可能携带带 `key=` 的 URL——建议对外只回类型+粗化文案，细节留日志。
5. **重试叠加**：`retry_call(3)` × OpenAI SDK 默认 `max_retries=2`，最坏 9 次调用数分钟无进度——给 client 显式 `max_retries=0` 由 retry_call 统一管。
6. **QPS 节流无锁**：editor 的 `_last` 读改写在多线程下可能同时放行（突发超 3 QPS）——把「算等待+记时间戳」放进一把线程锁。
7. **pick_poi 兜底**：全部候选都是干扰类型时仍回 `candidates[0]`，可能把地铁站加进行程——应返回 None 走「没搜到」。
8. **仲裁缓存存失败**：aligner 把仲裁失败的空结果也永久缓存，一次 LLM 抖动让该别名增强永久失效——只缓存成功结果。
9. **负面缓存**：评价生成失败写 `None` 无标记，每次规划重烧一次 LLM（`"reviews" in m` 的判据才是对的）。
10. **前端杂项**：`window.cityMismatch` 死状态、`qn` 死代码、`pin_modify_day` 仅测试引用（两套时间线重算易漂移）、`/plan/async` 不检查 `r.ok`、日历禁用格只禁了数字子元素、`dayCount` 截 7 天但日历可选更长、天气失败不清 `weatherData`、收藏切换双份全量渲染。

已知设计限制（非 bug，HANDOFF 已记录）：高德不返回票价（未知≠免费的口径已做）；停留时长是演示近似值；高德栅格瓦片仅限本地演示。

## 五、接下来的方向（建议优先级）

1. **把这两轮改动 commit 掉**（尚未提交）。建议按主题拆分：security(task_id+XSS)、async(spawn+to_thread+deepcopy)、media-cache(锁下沉)、api-contract(cost_known+/extract body)、frontend(smoke+css+ws)。拆开的好处是回看与回滚都清晰。
2. **Low 清单扫尾**（上表 10 项约半天量级），顺手补两条冒烟断言（日志去重已补，可再加收藏并发与灯箱计数）。
3. **给前端异步状态补测试**：本轮修的 6 个前端 bug 全是「注释完备但回归没覆盖」的盲区，建议 frontend_smoke 继续扩：WS onclose、pickHotel 超时、showLoading 取消。
4. **鉴权与多用户**：全站无鉴权是演示定位的刻意取舍，但若要公开部署，收藏/历史是全局共享 + LLM/高德可被任意烧配额，需要加最简 Token 鉴权 + 速率限制（输入上限已就位）。
5. **solver_cpsat 转正评估**：CP-SAT 求解器已有完整实现与测试（ortools 在 eval 依赖里），多起点贪心 gap 已压到 0~1%；如果未来实例规模变大，可评估把 CP-SAT 作为可选后端（importorskip 已兜底）。
6. **续接 HANDOFF 的口径**：本次所有修复均已遵守项目纪律（降级必留痕、不编造数据、票价未知≠免费）；HANDOFF.md 可追加一节「2026-09-28 外部审查修复记录」指向本报告。

---
*报告生成：ZCode（GLM），2026-09-28。改动明细可直接 `git diff` 查看，全部未提交。*
