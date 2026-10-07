# 前端「学设计」方法论：抓源码，不要凭理解发明

> 写于 2026-10-08 ｜ 起因：用户批评「你一点都没有运用我发给你的前端设计」
> 场景：为 `static/support.html` 重做「液态玻璃 × 工程感」风格
> **触发代价**：不抓源码，我猜错了四个组件的实现（见 §3），返工两轮。

---

## 0. 一句话

**截图告诉你「长什么样」，源码告诉你「怎么做的、有哪些参数可调」。
只有截图 ⇒ 你会凭理解发明，然后做出来「像但不是」。**

---

## 1. 三样东西各管一半，缺一样就返工

| 手段 | 回答什么 | **不回答什么** |
|---|---|---|
| **截图** | 长什么样、整体观感 | 怎么实现的 |
| **`getComputedStyle` 实测** | 最终值是什么（真实 blur/saturate/radius） | **结构**——哪个元素承担动画、参数怎么给的 |
| **源码 / Props 参数表** | **怎么做的 + 哪些参数可调** | — |

**实测发现**：只做前两项时，四处猜错（§3）。加上第三项后全部照抄对齐。

---

## 2. 抓源码的入口（照抄可直接用）

| 站点 | 组件页 URL | 源码在哪 |
|---|---|---|
| **Magic UI** | `magicui.design/docs/components/<组件名>` | 页面底部有 **Props 参数表** + Usage + Examples；源码路径 `@/registry/magicui/<组件名>`（在 demo 的 `import` 里能看到） |
| **Aceternity** | `ui.aceternity.com/components/<组件名>` | 组件页内代码块 |
| **Cult UI** | `cult-ui.com/docs/components/<组件名>` | 组件页内代码块 |
| **liquid-glass-react** | GitHub README | **参数表最全**（prop / 类型 / 默认值 / 说明）+ 浏览器支持限制 |

**抓取提示词模板**（可直接复制）：

> 提取这个组件的完整实现源码。我需要：1) 组件代码；2) 它实现 XXX 的具体做法
> （CSS 变量名 / 渐变写法 / 事件处理 / 哪个元素承担动画）；3) 参数表（每个参数的默认值）；
> 4) 依赖的库。

**注意**：Magic UI 与 Aceternity 的**首页是产品页**，组件效果在 `/components` 下。
抓首页只会得到 testimonials 和 spinner（我首轮就踩了）。

---

## 3. 实测证据：只抓截图猜错了什么

| 组件 | 我凭理解写的 | **源码实际** |
|---|---|---|
| **Dot Pattern** | CSS `radial-gradient` 平铺 | **SVG `<pattern>`** `width16 height16 x0 y0 cx1 cy1 cr1`，另有 `glow` 开关 |
| **Grid Pattern** | 两条 CSS `linear-gradient` | **SVG `<pattern>`** **`width40 height40 x-1 y-1`**，支持 `squares` 数组 + `strokeDasharray` |
| **Border Beam** | 两段 `conic-gradient` + mask 挖边 | **三段渐变** `from-transparent via-x to-transparent`，**`borderWidth` 是独立 prop**（默认 1） |
| **Spotlight** | 固定 `220px radial-gradient` | **MagicCard 两层**：跟随鼠标的**渐变边框**（gradientFrom→gradientTo）+ 内部柔光；`gradientSize200 / gradientOpacity0.8` |

**两条可推广的规律**：

1. **纹理类背景（点阵/网格/斜纹）必须用 SVG `<pattern>`**。
   用 CSS 渐变平铺「看起来像但调不出来」—— 圆半径、x/y 偏移量、虚线间距都没有对应旋钮。
2. **光效类（spotlight / glow card）至少两层：边框 + 内部**。
   只做一层就是塑料感。原版是「边框跟着鼠标走」+「内部柔光」两件事。

---

## 4. 参数先对齐原版默认值

原版 BorderBeam：`size50 / duration6 / borderWidth1`；
MagicCard：`gradientSize200 / gradientOpacity0.8`。

**先用原版默认值**，效果不对再调。
**一上来就自己定参数 = 又回到「凭理解发明」。**

---

## 5. 套用时的三个技术坑

### ① SVG data-URI 里不能用 `var()`
但**可以用 `currentColor`** —— 把颜色绑到元素的 CSS `color` 上，
令牌控制 `--grid-line` / `--dot`：

```css
.bg-grid {
  color: var(--grid-line);          /* ← 令牌 */
  background-image: url("data:image/svg+xml;utf8,<svg …><path stroke='currentColor'/></svg>");
}
```

这样既照抄了原版结构，又不硬编码色值 —— 能过
`grep "#[0-9a-fA-F]\{3,6\}" static/support.html` 只命中令牌区那道门禁。

### ② 抄动画要抄「哪个元素承担动画」
原版 shimmer 是 `button::after` 承担动画、button 本体不动。
我挂到 button 本体 ⇒ **整个按钮左右平移**（用户报「发送键一直在乱晃」）。
**按钮是位置敏感的交互元素，在动 = 点击目标在移动 = 误触漏触**，比难看严重一个量级。

### ③ 循环动画要配 `prefers-reduced-motion` 且**成对冻结**
「确保 X 没发生」的断言可能恒绿（万一本来就没 X）。
⇒ **成对验证**：reduce context 下 `animationIterationCount==='infinite'` 为 0，
**且**正常 context 下 ≥2。两条一起才有效。

---

## 6. 检查清单（动手前 / 动手后）

**动手前**
- [ ] 组件页抓到了吗？（不是首页）
- [ ] Props 参数表抄下来了吗？
- [ ] 哪个元素承担动画？（原版结构，不是我的理解）
- [ ] 默认值是多少？

**动手后**
- [ ] 用 `getComputedStyle` 核对实际值与原版默认值一致？
- [ ] `grep "#[0-9a-fA-F]"` 只命中令牌区？
- [ ] 同屏 infinite 动画 ≤2？（`getComputedStyle` 数，不是凭印象）
- [ ] reduce 模式**成对**验证过（冻结 + 对照组仍在跑）？
- [ ] 截图给用户看过**明暗两态**？

---

## 7. 素材落盘约定

抓下来的东西**一律落盘**，不要只看一眼：

```
sessions/<日期-时间>_<项目简称>/study/
├── <站名>.png / -dark / -full     截图（含暗色）
├── probe-<站名>.json               getComputedStyle 实测（圆角/动画名/同屏 infinite 数）
├── <站名>.css                      样式表里挑出的配方原文
├── <组件名>.md                     源码 + 参数表 + 我的改法对照
└── README.md                       文件清单说明
```

探针脚本也落盘（可复跑）。**别让「学习成果」只存在于一次对话里。**

---

## 8. 相关

- `docs/design/ui-design-system.html` —— 本项目设计规范
- `docs/design/contrast-audit.py` —— 对比度门禁（玻璃上文字按最坏底色算）
- `COLLAB_LOG.md` —— 每次套用组件的实测数字与取舍