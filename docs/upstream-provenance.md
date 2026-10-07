# 上游素材出处登记（support.html）

> 依据：`修改简报-20261008-support-直用上游素材-v5.md` §1「登记上游关键值 → 判路径」。
> 口径：**上游有现成实现的，直接套用上游原件；本项目是无构建单文件页，无法 `pnpm dlx shadcn add`，
> 所以走「逐值移植」这一条合法路径——但每一个值都必须查得到上游出处并登记在此。
> 查不到出处的，视为自创，删除或补齐出处。**

## 0. 素材根目录（vendor 里的上游原件）

- 技能（DSH）：`%APPDATA%\@deepseek-ai\dsh-desktop\harness\skills\ui-style-kit\`
- ZCode / WorkBuddy 各有 junction 指向上面的目录：`%USERPROFILE%\.zcode\skills\ui-style-kit`、`%USERPROFILE%\.workbuddy\skills\ui-style-kit`
- 上游原件（MIT，随技能分发）：`<技能>\vendor\magicui\components\*.tsx`、`<技能>\vendor\magicui\r\*.json`（registry 原件）、`<技能>\vendor\cult-ui\...`、`<技能>\vendor\tweakcn\...`
- 要重新抓站（上游更新时）：`node <技能>\assets\grab-registry.mjs magicui --names border-beam,shimmer-button,marquee,magic-card --out .\grab\magicui`
- 本页用到的四处效果对照表：`<技能>\reference\50-上游实现对照表.md`；纪律与验收：`<技能>\reference\30-纪律与验收.md`

## 1. 落地对照表（support.html 当前 1442 行）

| 效果 | 上游出处（vendor 原件） | 上游默认值 | 本页落点 | 判定 |
| --- | --- | --- | --- | --- |
| **Border Beam**（握手徽章走光） | `vendor/magicui/components/border-beam.tsx:54-105` | `size=50`(px) / `delay=0` / `duration=6`(s) / `colorFrom=#ffaa40` / `colorTo=#9c40ff` / `borderWidth=1` / `offsetPath=rect(0 auto auto 0 round 50px)` / `offsetDistance 0%→100%` 线性 / **DOM：带 mask 的 ring 为父、光点为子** | CSS `support.html:470-496`；DOM 在 `addBadge()` `support.html:1105-1120`（`ring=1111`、`dot=1114`） | 逐值移植（颜色偏离 1 处） |
| **Shimmer Button**（发送键扫光） | `vendor/magicui/components/shimmer-button.tsx:19-28`（参数）+ `:45-91`（四层 DOM） | `shimmerColor=#ffffff` / `shimmerSize=0.05em`（→ `--cut`）/ `shimmerDuration=3s` / `borderRadius=100px` / `background=rgba(0,0,0,1)` / `--spread:90deg` / spark 容器 `-z-30 blur-[2px] @container-[size]` / spark `h-[100cqh] aspect-[1]` + `animate-shimmer-slide` / `::before -inset-full` + conic + `spin-around`（时长 = speed×2 = 6s）/ Highlight `shadow-[inset_0_-8px_10px_#ffffff1f]`、hover `-6px`、active `-10px` / backdrop `inset-(--cut) -z-20` | CSS `support.html:544-605`（变量 544、四层 546-577、keyframes 579-589、禁用 599-601、reduce 602-605）；HTML 层 `support.html:801` | 逐值移植（圆角/底色偏离 2 处） |
| **Marquee**（问答条滚动） | `vendor/magicui/components/marquee.tsx:36-74` | `repeat=4`（默认）/ 容器 `flex gap-(--gap) overflow-hidden p-2 [--duration:40s] [--gap:1rem]` / 每条轨道 `flex shrink-0 justify-around gap-(--gap) animate-marquee` / keyframes `to{translateX(calc(-100% - var(--gap)))}` / hover 暂停 | CSS `support.html:615-632`（轨道组 621、hover 暂停 623、reduce 630-632）；JS `renderMarquee()` `support.html:979-997`（复制 **4** 份，原为 2 份） | 逐值移植（保留本页 mask 渐隐） |
| **MagicCard Spotlight**（运营行聚光） | `vendor/magicui/components/magic-card.tsx:57-67`（参数）+ `:159-195`（三层） | `gradientSize=200` / `gradientOpacity=0.8` / `gradientFrom=#9E7AFF` / `gradientTo=#FE8BBB` / `gradientColor=#262626` / ② `radial-gradient(200px circle at X Y, from, to, var(--color-border) 100%) border-box` / ③ 光层 `inset-px z-30`、hover 显形 300ms | CSS `support.html:665-698`（`::before` 681-690、`::after` 691-696、hover 697-698）；JS 只写 `--x/--y`（等价原版 `useMotionValue`+`useMotionTemplate`），注释 `support.html:1321` | 逐值移植（颜色/节奏/reset 偏离 3 处） |

## 2. 偏离登记（每一处都给理由；无可查出处的一律不算）

| # | 落点 | 上游默认 | 本页取值 | 理由 |
| --- | --- | --- | --- | --- |
| 1 | Border Beam 颜色 `support.html:468-471` | `#ffaa40 → #9c40ff`（橙→紫） | `var(--accent-solid) → var(--accent-deep)` | 本页是近单色蓝调系统，橙紫彩带与全站调色板冲突；形态（1px 边框环 + 走光）与上游一致 |
| 2 | Shimmer 圆角/底色 `support.html:532-544` | `border-radius:100px`、`background:rgba(0,0,0,1)` | `--radius-pill`、`--accent-solid` | 圆角 100px 与 pill 等价（按钮高 44px ⇒ 100px 已被钳成胶囊）；黑色底在亮色页面上等于反色，改用主色 |
| 3 | Shimmer Highlight 内阴影色 | `#ffffff1f` | 亮色态保持 `#ffffff1f`；hover/active 追加 `#ffffff3f`（`support.html:568-569`） | hover/active 强度阶梯上游用 tailwind 类表达、无对应 16 进制值；`#ffffff3f` 是同一白色的 25% 版本，属同族，已登记白名单 |
| 4 | Spotlight 色停 `support.html:679-680` | `#9E7AFF` / `#FE8BBB` / `#262626` | `var(--accent)` / `var(--note-line)` / `--border`（第三停）、`--fg`（光层） | 同 1：紫粉彩不属本页色系；**关键是三点结构与位置逐值对齐**（第三停是常态描边色、不是 transparent） |
| 5 | Spotlight 过渡时长 `support.html:683/693` | `300ms`（原版 `transition-colors`/Motion 默认） | `.18s` | 与全站 hover 节奏统一（150–180ms），避免一处独慢 |
| 6 | Spotlight 复位 | 原版监听全局 `pointerout`/`blur`/`visibilitychange` 复位 | 纯 CSS：鼠标离开即 `opacity:0`（`support.html:697-698`） | 视觉等价、零 JS；原版的全局监听是为了处理「鼠标移出窗口」的残留，本页聚光层级低（`.ops-row` 内），残留不可见 |
| 7 | 玻璃 `backdrop-filter` `support.html:263/291/387` | 上游默认 `blur(6px) saturate(140%)` | `blur(14px)`、`saturate(140%~180%)` | **项目自有偏离**（非上游移植项）：玻璃底下叠了三团流体色斑 + 40px 网格 + 噪点，6px 糊不住；暗色可读性另有 `.msg::after` 暗化层约束（见 COLLAB_LOG「暗色物理约束」条） |
| 8 | Marquee 份数 2 → 4 | 默认 4 | 4 | 原先的 2 份是自创：单份轨道 737px ⇒ 2 份 1474px < 1920px 桌面视口，**宽屏会露出空档**；4 份 2979px 才铺满，且位移量 = 单份宽 737 + gap 16 = 753px ⇒ 无缝 |

## 3. 不移植清单（说明为什么本页没有它们）

| 素材 | 为什么不用 |
| --- | --- |
| `retro-grid` / `meteors` / `aurora` 等背景特效 | 本页背景层已由 v4 定稿（三团色斑 + 网格 + 点阵 + 噪点），再加特效违反「循环 ≤2、玻璃是点睛不是地基」的纪律 |
| `fluted-glass`（cult-ui） | 依赖 `@paper-design/shaders-react`（WebGL shader）⇒ 无构建单文件页装不了 |
| `liquid-glass-react` 完整版 | 需要 React + SVG `feDisplacementMap`；纯 CSS 的 `backdrop-filter` 无法做出真折射 ⇒ 本项目只借用其「玻璃令牌 + 层次」思路，未引入组件 |
| `dot-pattern`（magicui） | 本页点阵已用内联 SVG 实现（`support.html:216-227`），等价且无依赖 |
| 其它 23 个已 vendor 组件（`dock` / `bento-grid` / `number-ticker` / `text-animate` …） | 本页没有对应交互位；需要时按 `<技能>\reference\50` 的对照表取值，或在有构建的项目里直接 `pnpm dlx shadcn@latest add https://magicui.design/r/<name>.json` |

## 4. 复验证据（本次改动后实测）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 上游移植层自检（技能自身） | `node <技能>\verify-demo.mjs` | `verdict PASS`、`fails []`（glass=2 / 循环元素=2 / 对比度 16.14·4.99·17.3；dark 13.76·6.29·14.66；reduce 下 `≈1e-05s` 且 `iteration-count:1`） |
| 本页运行时画像 | `node <技能>\assets\audit-page.mjs static\support.html` | `theme=light`、`glassCount=26`、`loopCount=5`、`minRatio=6.55`、`bareHex=(空)`、`pageErrors=(空)` |
| 同上，reduce | 加 `--reduce` | `loopCount=1`（只剩背景 blob；beam/shimmer/marquee 全部关闭） |
| Border Beam 的 mask 是否真裁到 1px 边框 | `tmp\verify-beam-mask.mjs`（冻结动画、把光点停在 0/25/50/75%，对比内部真空区与顶边带的像素哈希） | 内部真空区 **4 帧哈希完全一致**（=`maskClipsInterior:true`）、顶边带 **4 帧各不相同**（=`beamVisibleOnEdge:true`）⇒ `PASS` |
| 四处效果的运行时结构 | `tmp\verify-fx.mjs` | beam：`nested:true`、`maskComposite:intersect`、`dot 50×50`、`beam-run/6s/infinite`；shimmer：四层齐、`containerType:size`、`shimmer-slide/3s/alternate`、`::before spin-around/6s`、按钮 `transform:none`、高 44px；marquee：`groupCount=4`、`40s`、`gap 16px`、`scrollWidth 2979`（溢出容器 = 铺满）、`shiftPerCycle 753`；spotlight：idle `0/0` → hover `1/0.8`、第三停是实色 |
| 项目静态门禁 | `python tools\check_frontend.py` | ✅ 通过（exit 0） |
| 项目对比度门禁 | `python tools\check_contrast.py --all` | ✅ 通过（exit 0） |
| 项目冒烟 | `node tools\support_smoke.js` | **78 ✓ / 0 ✗**；脚本随后自行 `TypeError: fetch failed`——**既有环境问题**：`support_smoke.js:475` 把 `.catch()` 挂在了 `.text()` 上，`fetch` 本身没兜住，后端不在 `127.0.0.1:8000` 时必崩 |

**glassCount 26 的构成（诚实披露代价）**：`nav.site-nav` 1 处 + `.mchip` 快问按钮 **24** 处 + 1 处背景层。
24 = 6 个按钮 × **4 份 marquee 复制**（改动前 2 份 ⇒ 12 个，全页 14）。这是「marquee 必须 4 份才铺满宽屏」的直接代价：
每个 `.mchip` 都带 `backdrop-filter:blur(10px)`，同时运动的模糊元素从 12 增到 24。
如果后续帧率吃紧，可先把 `.mchip` 的 `backdrop-filter` 降级为纯半透明底（视觉差别很小），而不是减少份数（减少份数会露空档）。

## 5. 这份文档怎么用

1. 谁再改这四处效果：先看第 1 节的「上游出处」并在改动后更新行号；
2. 想加新效果：先在 `<技能>\reference\50` 找现成实现 → 有构建就用 registry，无构建就按第 1 节的方式逐值移植 + 在此登记；
3. **一行代码都查不到出处的「效果」，就是自创，删掉。**