# 大麦模式 0 · 真机调试说明

模式 0 在三台真机上运行：**前置连点 + 状态驱动提交**。
主程序只执行：**演唱会详情页 → 票档处理 → 确认购买 → 立即提交 → 支付宝收银台**，
检测到支付宝成为前台 App 后立即退出，不校验金额，也绝不点击支付宝"付款"。

每次执行前必须由操作者把手机停在目标演唱会详情页。程序不会返回、搜索或整理页面。

---

## 一、环境

| 设备 | 序列号 / 标识 | 脚本 | 校准分辨率 | 工具 |
| --- | --- | --- | --- | --- |
| Android 主力机 | `8595251f` | `scripts/damai_mode0.py` | `1440×3200` | adb |
| REDMI K90 至尊版（M332BF） | `f109b923` | `scripts/damai_mode0_K90.py` | `1280×2772` | adb |
| HUAWEI MatePad Air（HarmonyOS 6.1） | —— | `scripts/damai_mode0_harmony.py` | `1840×2800` | hdc |

- Python 环境：`.venv-phone`（仓库根目录）
- ADB：`E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe`
- hdc：`E:\soft-data\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe`

---

## 二、模式 0 的共同链路

### 2.1 预检

1. 校验屏幕尺寸与脚本常量**完全相等**，不一致直接报错（坐标安全的前提）；
2. 校验当前前台是大麦，否则报错"请先将大麦置于前台并停在目标演唱会详情页"。

### 2.2 时间轴

用 `time.perf_counter()` 单调钟锚定，不受系统时间调整影响：

```text
sale_monotonic = 现在 + (开售时间 - 现在)
click_start    = sale - 2.0s      # PRECLICK_LEAD_S：开售前 2s 起连点
first_probe    = sale + FIRST_STATE_CHECK_AFTER_S
deadline       = sale + 600s      # MAX_RUNTIME_AFTER_SALE_S：开售后最多 10 分钟
```

启动时若已过 `deadline`，返回 `timeout_before_start` 且**不发出任何点击**。

### 2.3 双线程分工（Android 双机）

- **点击线程** `tap_loop`：唯一的 ADB 写入者，通过两条持久 shell 派发点击；
- **探测线程**：只读页面状态，把"该点什么"丢进单槽请求队列 `TapRequestQueue`，
  由点击线程消费。每条请求都是异步的，探测线程从不直接点击（鸿蒙版例外，见第四节）。

### 2.4 盲连点 → 状态闭环

**阶段 `blind`**：在拿到任何确认页 / 弹窗信息之前，**主操作点每 150ms 点一次**。
这个点是刻意挑的——**一个点同时落在三个按钮内**：详情页购票入口、票档面板「确认」、
确认页「立即提交」，所以不需要识别页面就能一路点过去。

**一旦探到 `order_page` 或 `retry_dialog`**，`controlled_event` 置位，**停止盲连点**，
改为一次状态一次点击的闭环：

```text
继续尝试弹窗 → 点一次「继续尝试」→ 等「立即提交」可点击 → 点一次「立即提交」
      ↑                                                        ↓
      └──────────── 服务端再次返回弹窗 ←────────────────────────┘
```

任一步骤只要点击失败，服务端会再次返回弹窗，循环自动重入；直到支付宝成为前台或超时。

### 2.5 结束条件（三台一致）

| 状态 | 触发条件 |
| --- | --- |
| `alipay_reached` | 支付宝成为前台 → 清空待点击请求后**立即停** |
| `terminal_no_retry_option` | 提交后连续 2 次只探到「返回重新选购」→ 停（**永不点这个按钮**） |
| `timeout` | 开售后满 10 分钟 |
| `timeout_before_start` | 启动时开售已过且超出兜底期限：未发出任何点击 |
| `error` | 工作线程异常，结果 JSON 附带异常类型 |

### 2.6 每次点击前的安全闸

**不是大麦前台就不点；前台未知（查询失败或解析不出包名）也不点。**
这是"绝不误点"的核心实现。检测到支付宝前台时先清空待点击请求，再退出。

---

## 三、Android 双机（`damai_mode0.py` / `damai_mode0_K90.py`）

两个脚本逻辑**完全一致**，仅设备序列号、屏幕尺寸、两个坐标、若干文案与线程名不同。

### 3.1 坐标

| 常量 | `8595251f`（1440×3200） | K90（1280×2772） | 来源 |
| --- | --- | --- | --- |
| `MAIN_ACTION_POINT` | `[1119, 3088]` | `[995, 2675]` | 三按钮交集 |
| `RETRY_DIALOG_POINT` | `[720, 1966]` | `[640, 1703]` | 「继续尝试」中心 |

「继续尝试」按钮实测范围 `[244,1891][1195,2041]`，点击其中心 `[720,1966]`。

### 3.2 K90 坐标校准（等比缩放 + 真机验证）

K90 为 `1280×2772`，坐标由旧设备 `1440×3200` 等比缩放：
X 比例 `1280/1440 = 0.8889`，Y 比例 `2772/3200 = 0.86625`。

- **主操作点**：`1119×0.8889 ≈ 995`，`3088×0.86625 ≈ 2675`。
  已在 K90 真机验证：票档页确认按钮 `btn_buy_view` 实测 bounds `[263,2603][1212,2746]`，
  中心 y=2674.5 与缩放点 y=2675 完全吻合，x=995 落在按钮范围内；
- **重试点**：旧 `[720,1966]` 缩放为 `[640,1703]`。旧「继续尝试」`[244,1891][1195,2041]`
  缩放后为 `[217,1638][1062,1768]`，中心 `(640,1703)`。

> K90 的重试点目前只有缩放推导，未经像主操作点那样的真机 bounds 复验，首次使用建议单独校准。

### 3.3 页面判别（在内存里做，不截图、不抓 XML）

只用 5 个已验证 selector，按当前 phase 决定查询顺序（例如 `retry_clicked` / `await_submit`
阶段优先查「立即提交」）：

| 页面状态 | 判别依据 |
| --- | --- |
| `retry_dialog` | `cn.damai:id/damai_theme_dialog_confirm_btn` 且 `clickable=true` |
| `terminal_dialog` | `cn.damai:id/damai_theme_dialog_cancel_btn` 且 `clickable=true`（且无上一条） |
| `order_page` | 文本「立即提交」且 `clickable=true` |
| `order_page_loading` | `cn.damai:id/order_activity_title` |
| `risk_verification` | `cn.damai:id/water_mark_view_id` |

「继续尝试」使用 `damai_theme_dialog_confirm_btn`；「返回重新选购」使用
`damai_theme_dialog_cancel_btn`。同时存在两个按钮时属于**可重试弹窗**（判为 `retry_dialog`）；
提交后连续两次仅检测到「返回重新选购」时，以 `terminal_no_retry_option` 停止。

### 3.4 两条持久 shell

- `PersistentTapShell`：一次 `adb shell` 常驻，`input tap` / `input keyevent 4` 从 stdin 灌入，
  fire-and-forget，省掉每次起进程的开销；
- `PersistentQueryShell`：另一条常驻 shell 执行
  `dumpsys window | grep -E "mCurrentFocus|mFocusedApp"`，只回传匹配行并以 `__EOT__` 结尾，
  避免把整份 dumpsys 拖回本地。

### 3.5 风控页处理（仅 Android 双机）

若提交后出现滑块验证，程序清空待点击请求并**只发送一次** Android 返回键（`KEYCODE_BACK`），
不操作验证码、滑块或刷新按钮；风控页持续存在时不会重复返回。
返回到「继续尝试」弹窗后，程序自动恢复「继续尝试 → 立即提交」闭环。该过程仍计入 10 分钟总超时。

### 3.6 实测性能

模式 0 不调用 `screenshot()`，也不调用 `dump_hierarchy()`。状态线程只查询当前阶段需要的
少量 selector，点击线程是唯一 ADB 写入者，并在每次点击前通过持久 shell 确认前台仍是大麦。

| 操作 | 实测 |
| --- | --- |
| 普通详情页单 selector | 中位数约 64–80ms |
| 持久前台查询（正确消费 `__EOT__` 后） | 中位数约 49ms，P95 约 66ms |
| 普通详情页完整排除式探测 | 中位数约 209ms |

结果 JSON 记录 `retry_dialog_hits`、`terminal_dialog_hits`、`risk_verification_hits`、
`risk_back_count`、`risk_wait_seconds`、`state_transitions` 和 `foreground_query_errors`。

---

## 四、鸿蒙 MatePad Air（`damai_mode0_harmony.py`）

HarmonyOS 6.1 设备，**无 adb**，走 hdc：开售前 2s 起固定主操作点连点，
开售 +2s 起叠加状态探测（点击触发 + 周期兜底，设备端 grep 零 recv），
自动应答「继续尝试」弹窗；支付宝前台或终态弹窗（仅剩「返回重新选购」）即停。
不读取 profile，不涉及场次 / 票档选择逻辑。

### 4.1 环境与常量（脚本顶部）

- 大麦：`cn.damai.hongmeng`；支付宝：`com.alipay.mobile.client`
- 校准分辨率：`1840×2800`（焦点窗口尺寸校验，不一致即报错）
- 主操作点 `[1646, 2670]`：以下三个按钮的**交集**（bounds 来自鸿蒙抓包分析），
  与 Android 版"一点覆盖三页"语义一致：
  - 详情页「立即预订」`[233,2636][1800,2737]`
  - 场次面板「确定」`[920,2602][1840,2701]`
  - 确认页「立即提交」`[1500,2620][1792,2719]`
- 重试点 `[919, 1489]`：「抢票人数太多啦，继续尝试别放弃」弹窗内「继续尝试」按钮中心
  （bounds `[678,1444][1161,1534]`，20 次抓包中弹窗态全部一致）。弹窗为模态，
  弹窗外点击落在遮罩上无副作用；与下方「返回重新选购」（`[610,1568][1229,1651]`）
  y 距 79px，不会误触。

### 4.2 与 Android 版的架构差异（由 hdc 生态决定）

- hdc **不支持交互式持久 shell**，每次命令用子进程执行（热连接约 0.1s）；
- 前台检测用 `aa dump -l`（约 0.17s），解析 `bundle name [X] … state #FOREGROUND`，
  由长驻输出流（设备端 `while … aa dump -l` 循环）每约 0.2s 推送；流失效时自动降级为
  每次点击前同步查询；
- 点击间隔约 **0.36s**（`uitest uiInput click` 耗时），Android 版为 0.15s；
- 状态探测为**点击触发 + 周期兜底**：点击线程每次点击后立即触发一轮探测
  （弹窗只会在提交点击失败后出现，所以点击即探最贴合），抢票期间探测近乎连续；
  `--state-check-interval`（默认 0.5s）是点击暂停时的周期兜底；
- **首次探测严格在开售 +2s 开始**：开售前点击产生的触发在闸门处挂起（不消费），
  闸门一开立即放行，因此开售前窗口（−2s ~ +2s）**零探测**；
- 页面分类为**纯文本判别**（no resource-id 可用）；抓包证实弹窗出现时确认页子树整体消失：
  含「继续尝试」→ 重试弹窗；含「返回重新选购」且无「继续尝试」→ 终态弹窗；
  含「立即提交」→ 确认页；含「立即预订 / 已预约 / 票档已选好」→ 详情页；否则 → 大麦其他页；
- **弹窗闭环**：探测到「继续尝试」→ **探测线程直接点击重试点**（dump 刚证明弹窗在，
  省去经点击线程的 flag 往返；点击线程同时继续点提交位，落在遮罩上无副作用；
  漏点由下一轮探测自愈）→ 恢复「立即提交」连点；弹窗再现则重复；
- **无风控 / 滑块处理**：鸿蒙版没有 `KEYCODE_BACK` 逻辑，结果 JSON 中也没有风控字段；
- 终止条件：支付宝成为前台（立即停），或探测到仅剩「返回重新选购」的终态弹窗
  （「对不起，您选购的商品库存不足，请重新选购」）→ **连续 2 次探测确认**（防误判，对齐
  Android 版；`--terminal-confirm-count` 可调）→ 状态 `terminal_no_retry_option` 立即停，
  **程序绝不点击「返回重新选购」**。

### 4.3 探测通路

两条路径，启动时选定一次：

- **grep 模式**（默认，启动自检通过时）：一条复合 hdc 命令在设备端
  `uitest dumpLayout -i` + 对四个关键文本 `grep -c`，**只回传计数、零 recv**，
  400KB JSON 不离开设备。中文 needle 用 `$'\xHH…'` 十六进制形式，规避控制台代码页风险；
- **recv 模式**（回退）：dumpLayout + `hdc file recv` + 本地 JSON 解析。

启动自检为只读操作：在设备端用 `printf` 写入中文 fixture 再 `grep -c` 往返验证，
失败则自动回退 `recv`。

### 4.4 鸿蒙真机校准结论（2026 实机测量）

| 项 | 实测 | 结论 |
| --- | --- | --- |
| `dumpLayout -i` | ~1.2s / 399KB | 探测用 `-i`（不合并窗口、不过滤节点，弹窗必含） |
| `dumpLayout -a` | ~8.3s | 附加字体属性，极慢，**禁用** |
| 无旗标 / `-b` bundle | ~1.2s | 与 `-i` 相当，无收益 |
| 跨进程点击 vs dump | 点击 0.34s 不被阻塞 | 探测与连点可完全并行（同 shell 会话内 `&` 会等后台进程是假象） |
| `hdc file recv` 400KB | ~0.09s | 很小；grep 路径仍省掉它（零 recv 更稳） |
| 设备端 `grep -c` 中文 | 直传与 `$'\x..'` hex 均可用 | 代码用 hex 形式，零编码风险 |
| 完整 grep 探测一轮 | ~1.42s | dump 1.2s + grep + hdc 开销 |

**延迟预算**

- 弹窗出现 → 点击「继续尝试」：约 **0.7~1.6s**
  （最多一轮进行中的探测 ~1.2s + 点击入队 ~0.1~0.4s，平均 ~1s；
  服务端失败响应 ~0.5~1.5s 发生在此之前，不计入）；
- 一轮失败循环（提交 → 弹窗 → 继续尝试 → 再提交）：约 **1.2~2.5s**
  （提交 0.36 + 服务端 0.5~1.5 + 识别 ~0.6 + 点击 ~0.2 + 关窗 ~0.2）。

**为什么不能再快**（实测结论）：

1. 弹窗检测的唯一传感器是 `uitest dumpLayout`（~0.9~1.2s 设备下限，无过滤/部分抓取接口），
   "弹窗出现 → 识别"的下限就是一轮 dump；
2. 盲交替点击（不检测、提交 / 继续尝试轮流点）实测**不可行**：无弹窗时 `(919,1489)`
   落在确认页「联系方式」行，会弹出「选择联系人」面板盖住屏幕阻断提交（真机验证）；
3. 服务端失败响应 ~0.5~1.5s 不可压缩。

### 4.5 息屏注意（重要）

`uitest uiInput` 注入的点击**不会重置屏幕超时**。抢票期间若息屏会锁屏，导致前台不再是
`cn.damai.hongmeng`——点击线程会安全停点，但抢票会失败。

开抢前建议：

```powershell
hdc shell power-shell timeout -o 1000   # 覆盖息屏为 1000s
hdc shell power-shell timeout -r        # 恢复
```

或手动在系统设置里开启常亮。

---

## 五、配置常量汇总

### Android 双机（`damai_mode0.py` / `damai_mode0_K90.py`）

| 常量 | `8595251f` | K90 | 含义 |
| --- | --- | --- | --- |
| `DEVICE_SERIAL` | `8595251f` | `f109b923` | 目标设备 |
| `EXPECTED_SCREEN_SIZE` | `(1440, 3200)` | `(1280, 2772)` | 预检强校验 |
| `MAIN_ACTION_POINT` | `(1119, 3088)` | `(995, 2675)` | 一点覆盖三个按钮 |
| `RETRY_DIALOG_POINT` | `(720, 1966)` | `(640, 1703)` | 「继续尝试」中心 |
| `ADB_PATH` | `E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe` | 同左 | adb 路径 |

两台**完全相同**的时序常量：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `PRECLICK_LEAD_S` | `2.0` | 开售前 2s 起连点 |
| `CLICK_INTERVAL_S` | `0.150` | 盲连点间隔 |
| `FIRST_STATE_CHECK_AFTER_S` | `0.0` | 开售即开始探测 |
| `STATE_CHECK_INTERVAL_S` | `0.0` | 不等，探测背靠背 |
| `RISK_STATE_CHECK_INTERVAL_S` | `0.25` | 风控页存在时的探测间隔 |
| `MAX_RUNTIME_AFTER_SALE_S` | `600.0` | 开售后 10 分钟 |
| `THREAD_JOIN_TIMEOUT_S` | `3.0` | 收尾 join 上限 |

### 鸿蒙

| 常量 | 值 |
| --- | --- |
| `HDC_PATH` | `E:\soft-data\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe` |
| `EXPECTED_SCREEN_SIZE` | `(1840, 2800)` |
| `MAIN_ACTION_POINT` / `RETRY_DIALOG_POINT` | `(1646, 2670)` / `(919, 1489)` |
| `DAMAI_PACKAGE` / `ALIPAY_PACKAGE` | `cn.damai.hongmeng` / `com.alipay.mobile.client` |
| `PRECLICK_LEAD_S` | `2.0` |
| `CLICK_INTERVAL_S` | `0.36` |
| `FIRST_STATE_CHECK_AFTER_S` | `2.0` |
| `STATE_CHECK_INTERVAL_S` | `0.5` |
| `TERMINAL_CONFIRM_COUNT` | `2` |
| `STREAM_POLL_S` / `STREAM_STALE_S` | `0.15` / `1.0` |
| `MAX_RUNTIME_AFTER_SALE_S` | `600.0` |
| `PROBE_DUMP_FLAG` | `-i` |

---

## 六、运行命令

运行前请手动把手机停在大麦目标演唱会详情页。

```powershell
# ① Android 主力机 8595251f
.\.venv-phone\Scripts\python.exe scripts\damai_mode0.py "2026-08-18 13:00:00"

# ② REDMI K90 至尊版 f109b923
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_K90.py "2026-08-18 13:00:00"

# ③ 鸿蒙 MatePad Air
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_harmony.py "2026-08-23 21:00:00"
```

鸿蒙版可选参数：`--first-check-after <秒>`（默认 2）、
`--state-check-interval <秒>`（默认 0.5）、`--probe-mode auto|grep|recv`（默认 auto）、
`--terminal-confirm-count <N>`（默认 2）。

---

## 七、安全边界

- 启动前校验手机分辨率和当前详情页购票入口；
- 三台设备使用互相独立的坐标组，坐标全部按屏幕尺寸校准；
- 不选择、不取消、不校验观影人；使用大麦自动带入的选择；
- 前台切换到支付宝即停止，不读取或校验支付金额；
- 不自动处理验证码或滑块；Android 双机识别风控页后**只返回一级**，再恢复重试闭环；
- **永不点击支付宝付款按钮**；
- **永不点击"返回重新选购"**。
