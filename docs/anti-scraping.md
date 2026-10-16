# 反爬机制与破解思路

这份文档记录 BOSS直聘「推荐牛人」页面的反爬细节、破解方法，以及**所有踩过的坑**。

**改动解析逻辑前请务必读完。** 这里的坑有一个共同特征：
**它们不会报错，只会安静地给出错误数据。** 第一版实现时，"候选人平均经验"
算出来是 15.2 年，修正后是 3.7 年 —— 如果没做统计量交叉验证，
整份筛选结果会建立在错误数据上。

---

## 一、清单页 vs 详情页

| | 清单页 | 详情页 |
|---|---|---|
| 位置 | 主文档 → `iframe[src*="/frame/recommend"]` | 推荐 iframe 内 → `iframe[src*="c-resume"]` |
| 渲染方式 | 普通 DOM | **WASM 绘制到 `<canvas>`** |
| 可读性 | 直接读 DOM | `innerText` 恒为空 |

清单页唯一的门槛是 **同源 iframe 穿透**（可以直接用 `contentDocument`，
不需要 CDP 的 frame 切换）。

---

## 二、详情页的三层反爬

```
1. 接口加密
   GET /wapi/zpjob/view/geek/info/v2
   → zpData.encryptGeekDetailInfo  (约 17KB 密文)
   → zpData.geekDetailInfo = null

2. WASM 解密
   加载 wasm-resume-container/v232/
   解密在 wasm 线性内存里完成，JS 侧拿不到明文

3. Canvas 渲染
   解密结果不写 DOM，直接 ctx.fillText 到 <canvas id="resume">
   <div id="resume" style="height: 3460px">
     <canvas width="678" height="999" style="transform: translateY(0px)">
   </div>
```

所以「读 DOM」和「抓 XHR」两条常规路都断了。

---

## 三、破解：监听绘制调用

核心洞察：**WASM 可以隐藏数据，但不能隐藏"它要画字"这件事。**
任何 canvas 文本渲染最终都要落到 `fillText`。

### 步骤

1. **钩住画字 API**（在 `c-resume` iframe 的 window 内）

   ```js
   const P = w.CanvasRenderingContext2D.prototype;
   ['fillText', 'strokeText'].forEach(n => {
     const orig = P[n];
     P[n] = function (text, x, y) {
       w.__cap.push({t: String(text), x, y});
       return orig.apply(this, arguments);
     };
   });
   ```

2. **滚动触发分片重绘**，每滚一步读取新增的绘制调用

3. **换算绝对坐标**：canvas 靠 CSS transform 做滚动，
   `translateY(T)` 里的 T 就是偏移量

   ```
   绝对 y = T + 局部 y
   ```

4. **还原版面**：
   - 按绝对 y 聚类成行（容差 6px）
   - 行内按 x 排序
   - 相邻文本的 x 间距超过阈值（约 14px）时插入列分隔
   - 按顺序去重（滚动重绘会产生重复行）

### 为什么这么做，而不是 OCR

- OCR 丢字段结构，还原不出「公司名 | 职位 | 起止时间」这种列关系
- OCR 有识别错误率，中文简历里的技术名词（`gRPC`、`Volcano`、`HNSW`）容易错
- 绘制调用是**精确的原始文本**，零识别错误

同样的思路适用于任何「渲染到 canvas 的内容」：地图、图表、
部分电子书阅读器，以及其它用 canvas 做渲染防抓的站点。

---

## 四、坑（重点）

### 坑 1：每次点开候选人都新建 iframe 并堆叠

**不是复用。** 点第 N 个人，DOM 里就有 N 个 `c-resume` iframe 留着。

```js
// ✗ 错误：拿到的是第一个人遗留的旧 iframe，表现为「0 次新绘制」
const f = [...doc.querySelectorAll('iframe')].find(x => x.src.includes('c-resume'));

// ✓ 正确：补丁打到所有 iframe（新开的那个才会重绘），读写只针对最后一个
const ifs = [...doc.querySelectorAll('iframe')].filter(x => x.src.includes('c-resume'));
const f = ifs[ifs.length - 1];
```

连抓几十人后 DOM 里会堆很多 iframe，内存上涨。
长时间运行建议分批、中间刷新页面。

### 坑 2：加粗文字被画两遍 → 字符重叠

加粗的渲染方式是 `fillText` + `strokeText` 在**完全相同的位置**各画一次。
两个都钩就会得到：

```
11、、担担任任开开发发者者，，编编写写PPyytthhoonn程程序序
```

**这个文本喂给 LLM 会严重降低筛选质量**，但肉眼扫一眼容易误以为是排版问题。

修法：按 `(round(x,1), round(y,1), text)` 精确去重。
第一个候选人 3738 次绘制调用里有 **512 组完全重复**。

### 坑 3：`idx` 不能用来定位详情，必须用 `geekid` ⚠️ 最危险

清单里混着非候选人卡片：

- **VIP 推广横幅** —— `当前职位可升级曝光效果 / 23:46:48 / 去升级`
- **「热搜牛人推荐」卡** —— 匿名姓名（`王**`），**没有 `data-geekid`**，
  属于另一种卡片类型

清洗时这些会被剔掉，于是**清洗后的 `idx` 与 DOM 里的卡片序号就错位了**。
照 `idx` 去点详情，会点到**别人**，返回**另一个候选人的简历** ——
而整个流程不会报任何错。

修法：用 `data-geekid` 定位（挂在 `.card-inner` 上），它是稳定的。

```js
const card = cards.find(c =>
  (c.querySelector('.card-inner') || c).getAttribute('data-geekid') === geekid);
```

**验证方法**：抓详情时把清单里的姓名和详情返回的姓名比对，不一致就是错位。
这个断言应该一直留着。

### 坑 4：`NN年应届生` / `NN年毕业` 不是工作年限

| 原文 | 错误理解 | 正解 |
|---|---|---|
| `26岁 27年应届生 硕士` | 27 年经验 | **2027 届**毕业生 → 0 年 |
| `29岁 22年毕业 硕士` | 22 年经验 | **2022 年**毕业 |
| `21岁 1年以内 本科` | 状态残留「以内」 | 文案是「1年以内」，需剥掉后缀 |

必须在解析工作年限**之前**先剥掉 `\d{2}年应届生` 和 `\d{2}年毕业`：

```js
const mGrad  = base.match(/(\d{2})\s*年\s*应届生/);
const mGradY = base.match(/(\d{2})\s*年\s*毕业/);
const mExp   = (mGrad || mGradY) ? null : base.match(/(\d+)\s*年(以上|以内)?/);
```

影响面：400 个候选人的样本里，**164 人**（41%）是应届生。
不修的话，「平均经验」会从真实的 **3.7 年** 变成 **15.2 年**。

### 坑 5：`.expect-wrap` 里混着标签行

`.expect-wrap` 的 `innerText` 有时是 `最近关注\n成都 Golang`，
而不是 `期望\n成都 Golang`。直接取第一行会把 `最近关注` 当成城市
（样本里污染了 170 条）。

修法：按行拆分，滤掉已知标签行
（`期望` / `最近关注` / `沟通过` / `看过我` / `新牛人`），取最后一个非标签行。

### 坑 6：Chrome 136+ 拒绝在默认 profile 上开调试端口

出于安全考虑，`--remote-debugging-port` 对**默认用户目录**直接失效，端口不开。
必须指定别的 `--user-data-dir`。这也意味着**复用不了你日常浏览器的登录态**，
需要在受控窗口里重新登录一次。

### 坑 7：`--no-sandbox` 的由来

如果 Chrome 是从一个受限沙箱（seatbelt 等）里启动的，
它**无法嵌套自己的多进程沙箱**，子进程全部
`sandbox initialization failed`，GPU 进程反复崩溃后直接
`FATAL: GPU process isn't usable. Goodbye.`（SIGTRAP，表现为"Chrome 秒退"）。

修法：从沙箱外启动（用户自己的 Terminal，**推荐**），
或加 `--no-sandbox --disable-gpu`（会牺牲浏览器进程隔离）。

---

## 五、验证清单

改任何解析逻辑后，跑这些检查：

```bash
# 1. 协议层没坏（不需要真浏览器）
python3 tests/test_cdp_client.py

# 2. 环境自检：能定位到具体哪一层坏了
python3 zhipin.py doctor --stdout

# 3. 详情抓取后校验姓名一致性 —— 防坑 3
#    （zhipin.py 已内置：结果的 card.name 与 name 应始终相等）
```

统计量交叉验证（**最容易被忽略但最重要**）：

- 平均工作年限是否落在合理区间（不应超过年龄 − 16）
- 应届生比例是否量级合理
- 城市分布是否与目标岗位所在地一致
- 学历分布是否合理

只要有一项离谱，就说明某个解析规则写错了 —— 而且它不会报错。

---

## 六、稳定性

页面结构改版会让以下选择器失效：

| 选择器 | 用途 |
|---|---|
| `li.card-item` | 清单卡片 |
| `.card-inner` + `data-geekid` | 稳定标识 |
| `.name` / `.salary-wrap` / `.base-info` / `.expect-wrap` | 基础字段 |
| `.tags .tag-item` / `.geek-desc` / `.work-exps` / `.edu-exps` | 详细字段 |
| `.resume-detail-wrap` | 详情弹层的滚动容器 |
| `.iboss-close` | 关闭弹层 |
| `iframe[src*="/frame/recommend"]` / `iframe[src*="c-resume"]` | 两级 iframe |

`doctor` 能快速定位是哪一层出的问题。这是所有 DOM 自动化的固有代价，
没有根本解法 —— 只能让失败**快速且可诊断**，而不是安静地出错。
