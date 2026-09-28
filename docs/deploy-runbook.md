# 上线实操手册（花生壳免费档 · 定位：朋友娱乐 + 作品集）

> 配套计划：`ai-trip 部署计划-花生壳免费档.md`（工作区根，ZCode 制定）。
> 本文是**执行版**：阶段一是代码（已完成，见下），阶段二~四需要本人在电脑上操作，逐步照做即可。
> 2026-09-28 由 workbuddy 整理，**含三处对原计划的修正**（见文末「与原计划的差异」）。

---

## 阶段一：代码侧闸门 —— ✅ 已完成（commit 见 git log）

| 做了什么 | 怎么验证 |
|---|---|
| 访问令牌：18 个有副作用/花钱的接口需要 `X-App-Token` 头 | `pytest backend/tests/test_auth.py -q` → 16 passed |
| WebSocket 走 `?token=`，失败 `close(4401)` | 同上（含 4401 用例） |
| 每 IP 限流 30 次/分钟（只计花钱路径） | 同上（含 429 用例） |
| `/health`、页面、`/static`、`/demo/*`、`/cities`、`/meta` 免令牌 | 同上（含「遍历真实路由表」的闸门用例） |
| 前端：令牌面板 + 统一注入（仅同源）+ 401/429/4401 提示 | `node tools/frontend_smoke.js` / `hotel_smoke.js` |

### 启用令牌（**上公网前必做**）

```bash
cd "D:\workby room\ai-trip-planner"
# 1) 生成一个令牌
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"

# 2) 写进 backend/.env（该文件已被 .gitignore 忽略，不会进仓库、不会进镜像）
#    APP_TOKEN=<上一步的输出>
```

**确认已上锁**：启动日志会打 `APP_TOKEN 已启用`；`curl http://127.0.0.1:8000/meta` 里
`token_required` 应为 `true`。若看到启动告警 `APP_TOKEN 未配置：接口**无鉴权**`，
说明还没生效 —— 别在这个状态下开隧道。

**怎么把令牌给朋友**：直接发给他，让他打开页面 → 右上角令牌按钮 → 粘贴 → 保存（存在浏览器 localStorage，一次即可）。

---

## 阶段二：本机按生产模式跑通

```bash
cd "D:\workby room\ai-trip-planner\backend"
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

- ⚠️ **用 `127.0.0.1`，不要用 `0.0.0.0`**（原计划写的是 0.0.0.0）——理由见文末「与原计划的差异」。

**或者用 Docker**（`Dockerfile` / `docker-compose.yml` 已就绪，无需改）：

```bash
cd "D:\workby room\ai-trip-planner"
docker compose up -d --build      # env_file 已把 backend/.env 注入，APP_TOKEN 会自动生效
docker compose logs -f            # 看启动日志里有「APP_TOKEN 已启用」
```

`docker-compose.yml` 的 `ports` 已收紧为 `127.0.0.1:8000:8000`（只绑回环）。
Dockerfile 里的 `--host 0.0.0.0` 是**容器内**绑定，属于正确写法 —— 容器自己的 loopback
从宿主机不可达，暴露面由 compose 的 `ports` 决定，两者配合才是"只对本机开放"。
- **Windows 电源**：设置 → 系统 → 电源 → 屏幕和睡眠 → 「睡眠」设为**从不**。
  笔记本另需把「合盖时」设为「不采取任何操作」，否则合盖就断网。
- 建议找台旧机/笔记本专门常开，别用你日常要背着走的那台。

### 本机自测清单（开隧道前自己先走一遍）

- [ ] 粘一段真攻略 → 规划成功（有高德 Key 走真实通勤；无 Key 走直线估算兜底 —— 两条都试）
- [ ] WS 进度条正常推进（不是一直 0%）
- [ ] 对话改行程（「明天下午加个咖啡馆」）
- [ ] 换酒店 / 一键重排（切偏好 → 「按新偏好重排」）
- [ ] 收藏、历史回看（`/plans`）
- [ ] **重启服务后历史规划仍在**（落盘在 `data/`，不应丢）
- [ ] 地图能出来（瓦片由浏览器直连高德 CDN，不走隧道）

---

## 阶段三：花生壳免费档（这一步必须你本人在场）

### 3.1 账号与隧道

1. 注册贝锐账号 → **实名认证**（免费档也要，绕不过）
2. 控制台 → 内网穿透 → 添加映射：
   - 应用类型：**HTTP**
   - 内网主机：`127.0.0.1`
   - 内网端口：`8000`
3. 安装并登录花生壳客户端（保持开机自启 + 常驻）
4. 记下分配的域名（形如 `xxxx.oicp.net`）——**免费档的域名是分配制，重建隧道可能变**

### 3.2 验证

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<你的域名>/health      # 期望 200（无需令牌）
curl -s -o /dev/null -w "%{http_code}\n" https://<你的域名>/favorites   # 期望 401（已上锁）
```

浏览器打开 `https://<域名>/app` → 右上角填令牌 → 走一遍规划。
HTTPS 由花生壳提供，前端的 `wss` 判断代码已就位（会按 `location.protocol` 自动切）。

### 3.3 拉朋友实测（收集三类反馈）

> 请 2~3 个朋友用**手机**试（微信内置浏览器优先），只问三件事：
> ① **打不开？** ② **卡/慢？** ③ **看不懂/不知道下一步点哪？**

反馈原样记下来（对话截图也行），比你自己猜有用得多。

### 3.4（可选）静态宣传页

花生壳 Drop 只能放纯静态页（拖 HTML 生成链接），跑不了后端。
如果要做，放一句介绍 + 指向真应用的二维码/链接即可，**别期待它承载功能**。

---

## 阶段四：上线配套（一次性）

- **高德控制台**：给 Key 设**日调用上限**（硬止损，比代码里的限流可靠 —— 它在平台侧生效，绕过不了）
- **LLM Key**：平台侧设额度上限（同上）
- **日志**：`backend/.env` 里设 `LOG_FILE=logs/app.log`，朋友报障先看日志再猜
- 可选：UptimeRobot 免费档打 `/health`（免令牌，本来就是给监控用的）
- 可选：`.env` 里调 `RATE_LIMIT_PER_MIN`（朋友多/带宽小就调小）

---

## 验收清单（全部勾掉才算上线完成）

- [ ] 无令牌 `POST /favorites` → 401；带令牌 → 200
- [ ] WS 无令牌 → 4401（浏览器 DevTools Network → WS，看关闭码）
- [ ] `GET /health` 免令牌 → 200
- [ ] 朋友手机上完整走通：粘攻略 → 规划 → 对话改 → 看地图
- [ ] 重启电脑/服务后，历史规划仍在
- [ ] 门禁全绿：`pytest backend/tests/ -q`、`ruff check .`、`mypy .`、
      `tools/check_frontend.py`、两个 node 冒烟、`run_demo.py`
- [ ] 高德/LLM 平台侧配额已设上限

---

## 已知限制（如实告知朋友，也写进 README）

- **家里电脑常开才有服务**，关机 = 下线（免费档的本质）
- 免费档带宽/流量有限，人多会慢；朋友圈规模（≤10 人）够用
- 免费隧道域名由花生壳分配，重建隧道可能换域名
- 高德不返回票价、停留时长是演示近似值 —— 前端已按「未知 ≠ 免费」如实显示

---

## ⚠️ 与原计划的差异（三处修正）

1. **绑定 `127.0.0.1` 而不是 `0.0.0.0`**。花生壳客户端跑在**同一台机器**上，连的就是本机
   `127.0.0.1:8000` —— 绑 `0.0.0.0` 对它没有任何额外好处，却把服务暴露给整个局域网
   （同一 WiFi 下任何人都能直连，还能绕过隧道伪造 `X-Forwarded-For`）。少一个暴露面。

2. **限流的 IP 取值要认 `X-Forwarded-For`**。这是原计划没提到的坑：隧道场景下所有请求都来自
   本机，`request.client.host` **恒为 127.0.0.1** —— 直接按它限流会把所有朋友算成**同一个人**，
   30 次/分钟 被全场共享（人一多就集体 429）。代码里已改为：直连方是回环时才采信 XFF，
   否则用真实 peer；而且**取 XFF 的最后一个值而不是第一个** —— 标准反代是**追加**式写入
   （`客户端自带的` + `, ` + `真实 peer`），第一个恰好是攻击者能伪造的那段，取它等于
   一个伪造头换一个限流桶、把限流做废。

   **上线后建议实测一次**（推断不能替代事实 —— 花生壳到底怎么写的这个头，只有试了才知道）：

   ```bash
   # 从隧道外面（比如用手机热点）发一个伪造 XFF 的请求，然后看服务日志里的限流/请求记录
   curl -H "X-Forwarded-For: 1.2.3.4" https://<你的域名>/health
   # 再在服务端日志里确认解析出的 IP：如果恒为 127.0.0.1 或恒定同一个值，
   # 说明花生壳没透传真实 IP —— 那么"每 IP 限流"实际是"全场共享一份额度"，
   # 对策：把 RATE_LIMIT_PER_MIN 调大到朋友规模不再误伤（如 200），或改用令牌级计数。
   ```

3. **`/docs` 与 `/openapi.json` 保持公开 —— 刻意为之，不是漏网**。首页 footer 有意链到
   Swagger UI（作品集要展示），文档只暴露接口形状、不含任何 Key，而文档里"Try it out"能点的
   花钱接口背后照样要令牌。若以后要收，做法是「`docs_url=None` + 自建受令牌保护的 `/docs`」，
   而不是直接关掉（那会把首页那个链接变成死链）。
   ⚠️ 顺带一提：Swagger UI 的 JS/CSS 是 FastAPI 默认从 CDN 拉的，与本站「零 CDN」的纪律不一致 ——
   属于既有情况，本次未动。

---

## 接手指南

- 代码闸门：`backend/main.py` 的 `verify_token` / `ws_token_ok` / `client_ip` / `rate_limit_*`
- 测试：`backend/tests/test_auth.py`（含「遍历真实路由表」的闸门用例 —— 新增写接口忘了加依赖会红）
- 前端注入：`static/index.html` 里包 `window.fetch` 的地方
- 接口变化提醒：受保护接口需要 `X-App-Token`；WS 需要 `?token=`
