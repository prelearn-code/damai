# 大麦真机极速流程

主程序只执行：**演唱会详情页 → 票档处理 → 确认购买 → 立即提交 → 支付宝收银台**。
程序检测到支付宝成为前台 App 后立即退出，不校验金额，也绝不点击支付宝“付款”。

## 环境

- 设备序列号：`8595251f`
- 校准分辨率：`1440×3200`
- Python 环境：`.venv-phone`
- 新格式配置示例：`profiles/linyi_li_ronghao_2026-08-18_13-00_mode1.json`
- 默认模式：模式 1

每次执行前必须由用户将手机停在目标演唱会详情页。程序不会返回、搜索或整理页面。

## 三种选择模式

## 独立模式 0：固定位置连点

模式 0 位于独立脚本 `scripts/damai_mode0.py`，不读取 profile，也不复用模式 1/2/3 的选择逻辑。
命令只传入本地开售时间，设备固定为 `8595251f`：

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_mode0.py "2026-08-18 13:00:00"
```

当前全局配置：

- 开售前 2 秒开始点击；
- 主操作点 `[1119, 3088]`，每 150ms 点击一次；
- 开售后 2 秒开始最小化状态查询，每 1 秒一次；
- 开售后 2 秒起，如果仍检测到确认购买页，就按模式 0 规则视为弹窗已拦截；下一次点击改为推断的“继续尝试”中心点 `[720, 1470]`，随后恢复主操作点；
- 开售后最多运行 10 分钟；
- 每次坐标点击前都检查前台 App，不是大麦时不点击；支付宝成为前台后立即停止。

时间和坐标常量集中在脚本顶部。`[720, 1470]` 是根据用户提供的照片按 1440×3200
推断的弹窗按钮中心，正式使用前应在目标真机弹窗上校准。脚本仍会强制校验手机分辨率，且绝不点击
支付宝付款按钮。

模式 0 不调用 `screenshot()`，也不调用 `dump_hierarchy()`。每次状态查询先读取前台包名；仍在
大麦时，只查询确认购买页的 `order_activity_title`，不再查询未知的弹窗按钮 ID。因此每轮最多只有
一个最小 UI 节点查询，不抓取或保存完整 XML。查询从开售后 2 秒开始：此时如果仍在确认购买页，
就按模式 0 的约定判定弹窗已拦截，安排一次“继续尝试”坐标点击，然后恢复主操作点连点。

### 模式 1：已预约，预约票价仍有票（默认）

打开票档后直接点击“确认”，进入确认购买页后直接点击“立即提交”。

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout.py `
  --profile profiles\li_ronghao_urumqi.json `
  --serial 8595251f `
  --selection-mode 1
```

从详情页倒计时等到开售，然后跑完模式 1：

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout.py `
  --profile profiles\linyi_li_ronghao_2026-08-18_13-00_mode1.json `
  --serial 8595251f `
  --selection-mode 1 `
  --wait-for-sale --fast `
  --sale-time "2026-08-18 13:00:00"
```

`--fast` 必须同时传入 `--sale-time`。启动时会读取页面的开抢时间并核对月、日、时、分，
不一致时立即退出，防止 profile 或详情页用错。

程序先确认倒计时节点稳定存在，再轮询其消失；连续两次消失即点击购票入口。每次下拉刷新后，
必须重新观察到倒计时节点，才会恢复节点消失检测，避免把刷新期间的临时卸载误判为开售。
定时器始终具有最高优先级：到达开售时间加 profile 的 `t_fallback_ms`（当前为 150ms）后，
不再等待或检查倒计时节点，立即点击购票入口并执行模式 1。

首次点击后若票档确认按钮尚未出现，只要详情页购票入口仍存在，就会每 100ms 重试一次；
确认按钮出现后立即停止重试。
运行结果中 `sale_trigger` 为 `signal` 或 `timer`，并包含倒计时重新武装次数、最后一次观察到
倒计时及最后一次刷新相对开售时间的位置、连续缺失时长和触发时是否已武装等诊断字段。
进入票档页后，模式 1 轮询 `btn_buy_view` 的 `clickable=true` 状态，出现后直接点击节点，无固定延迟。
进入确认购买页后，轮询文本为“立即提交”且 `clickable=true` 的节点，出现后直接点击。

提交后如果出现“抢票人数太多啦，继续尝试别放弃”弹窗，模式 1 会执行专用循环：

```text
继续尝试 → 弹窗消失 → 立即提交 → 再次等待结果
```

弹窗再次出现就重复上述流程，直到支付宝成为前台 App 或 `queue_timeout` 超时。
程序绝不点击“返回重新选购”。

## 模式 1 精确计时

正式抢票优先使用 `damai_checkout.py`。需要分析每一步的毫秒数据时，使用精确计时启动器：

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout_trace.py `
  --profile profiles\linyi_li_ronghao_2026-08-18_13-00_mode1.json `
  --serial 8595251f `
  --selection-mode 1 `
  --wait-for-sale --fast `
  --sale-time "2026-08-18 13:00:00"
```

`damai_checkout_trace.py` 与主程序执行相同的完整模式 1 链路，只在同一进程内附加计时。
数据实时写入 profile 的 `artifacts` 目录：

```text
precision-trace-YYYYMMDD-HHMMSS.jsonl
```

每条记录包含 `perf_counter_ns`、`elapsed_ms`和本地墙上时间，覆盖倒计时触发、购票点击、票档确认、
立即提交、继续尝试、去支付和支付宝到达。程序中途报错时也会保留已写入数据和错误栈。

### 为什么不同时保存每一步 XML/截图

- `dump_hierarchy()` 和 `device.screenshot()` 会与主流程共用 uiautomator2；
- 单次抓取可能耗时数百毫秒，会拖慢开售检测和点击；
- 正式抢票期间只记录 JSONL 精确时间，不抓 XML、不截图、不上传数据。

页面结构分析应在单独演练时使用 `phone_probe.py`，或在失败后保持页面不动再抓取。

### 模式 2：已预约，原票价无票，改选其他票价

打开票档后点击 `--target-price` 对应的替代票档，0.2 秒后确认；后续直接提交。

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout.py `
  --profile profiles\li_ronghao_urumqi.json `
  --serial 8595251f `
  --selection-mode 2 `
  --target-price 880
```

模式 2 当前保存了 380、400、480、500、580、680、880、1080 元的位置。实际使用前应在
“预约票价无票”的真实页面单独校准目标票档。

### 模式 3：未预约，需要选择第二场和票价

等第二场控件真实出现后点击第二场，0.2 秒后点击滑动中的 880，0.1 秒后确认；后续直接提交。

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout.py `
  --profile profiles\li_ronghao_urumqi.json `
  --serial 8595251f `
  --selection-mode 3 `
  --target-price 880
```

## 只测试到确认购买页

在任一模式命令末尾增加 `--stop-before-submit`，程序将停在确认购买页，不点击“立即提交”，
也不会打开支付宝。

## 当前关键延迟

- 第二场控件稳定：0.02 秒
- 模式 3，第二场 → 滑动中的票价：0.2 秒
- 模式 2，替代票价 → 确认：0.2 秒
- 模式 3，票价 → 确认：0.1 秒

## 安全边界

- 启动前校验手机分辨率和当前详情页购票入口。
- 三种模式使用互相独立的坐标组。
- 不选择、不取消、不校验观影人；使用大麦自动带入的选择。
- 前台切换到支付宝即停止，不读取或校验支付金额。
- 不处理验证码、滑块或平台风控。
- 永不点击支付宝付款按钮。
