# AGENTS.md — 本项目对 AI 的硬性限制

本文件是 AI 在本仓库内工作的行为约束。优先级高于一般性建议；与本文件冲突的修改一律不做，
除非用户明确、逐条地推翻本文件。

## 项目是什么

大麦网演唱会抢票自动化，**只保留模式 0**：Android 真机（adb + uiautomator2）与
HarmonyOS 设备（hdc）上的「前置连点 + 状态驱动提交」。

主程序只执行：
**演唱会详情页 → 票档处理 → 确认购买 → 立即提交 → 支付宝收银台**，
检测到支付宝成为前台 App 后立即退出。

模式 0 **不读取任何 profile 配置文件**：命令行只传一个本地开售时间，
坐标与时序全部硬编码在脚本顶部常量里。

### 安全红线（永不可违反）

- **绝不点击支付宝的"付款 / 确认支付"按钮**，程序不校验金额；
- 程序不返回、不搜索、不整理页面；每次执行前由操作者手动把手机停在目标演唱会详情页；
- **永不点击"返回重新选购"按钮**（终态弹窗的唯一出口，点它等于放弃抢票）；
- 不自动处理验证码或滑块；Android 双机识别风控页后**只返回一级**，不得改成连续返回或点击。

## 限制 1：三台设备的坐标与屏幕尺寸（强制）

| 设备 | 脚本 | 校准分辨率 |
| --- | --- | --- |
| `8595251f`（Android 主力机） | `scripts/damai_mode0.py` | `1440×3200` |
| `f109b923`（REDMI K90 至尊版） | `scripts/damai_mode0_K90.py` | `1280×2772` |
| HUAWEI MatePad Air（HarmonyOS） | `scripts/damai_mode0_harmony.py` | `1840×2800` |

- 三台设备使用**互相独立的坐标组**（`MAIN_ACTION_POINT`、`RETRY_DIALOG_POINT`），
  不得跨设备复用，不得凭经验"估算"一个新坐标；
- 新坐标必须在**目标真机上实际校准**，以实测 bounds 推算中心点，并把 bounds 写进注释；
- **任何坐标相关改动都必须保留屏幕尺寸校验**（`EXPECTED_SCREEN_SIZE` 不一致即报错），
  这是所有坐标安全的前提，不得删除、不得放宽为"近似匹配"。

## 限制 2：默认抢票链路（强制）

除非用户**主动、明确**提出改变（例如要求改连点间隔、改探测方式、改结束条件），
否则三台设备的模式 0 行为**一律不得改动**：

1. 开售前 2s（`PRECLICK_LEAD_S`）起，主操作点按 `CLICK_INTERVAL_S` 盲连点；
2. 主操作点必须是"一点覆盖三页"（详情页购票入口 / 票档面板确认 / 确认页立即提交）的那个点；
3. 探到确认页或重试弹窗后停止盲连点，改为一次状态一次点击的状态闭环；
4. 结束条件不变：支付宝前台即停 / 连续 2 次终态弹窗即停 / 开售后 10 分钟超时；
5. 每次点击前必须检查前台是否为大麦，**前台未知时不得点击**（这条安全闸不得弱化或移除）。

"用户不主动提出来，就默认不变"——不允许 AI 自行发挥、猜测或"优化"这条链路。

## 限制 3：仓库只保留模式 0

`scripts/` 下只应有以下内容，不得新增其它模式、抓包脚本或调试脚本：

```text
scripts/
├── damai_mode0.py             # 模式 0 入口：Android 8595251f（1440×3200）
├── damai_mode0_K90.py         # 模式 0 入口：REDMI K90 至尊版（1280×2772）
├── damai_mode0_harmony.py     # 模式 0 入口：鸿蒙 MatePad Air（1840×2800）
├── damai_android_tail.py      # 两个 Android 入口共用的确认页后状态机（运行期依赖）
├── damai_mode0_gui.py         # 【已 gitignore】EXE 的 GUI 入口
└── build_damai_mode0_exe.ps1  # 【已 gitignore】EXE 打包脚本
```

- `damai_android_tail.py` 是 `damai_mode0.py` 与 `damai_mode0_K90.py` 的**运行期依赖**，
  删除或改名会让两个 Android 入口无法启动；
- EXE 构建相关代码（`damai_mode0_gui.py`、`build_damai_mode0_exe.ps1`）与构建结果
  （`dist/`、`build/`、`*.spec`）**已在 `.gitignore` 中忽略**，本地保留、不上传，
  不得把它们从 ignore 列表移出。

## 目录结构

- `scripts/damai_mode0.py` — 模式 0 入口：Android `8595251f`（1440×3200）
- `scripts/damai_mode0_K90.py` — 模式 0 入口：REDMI K90 至尊版（1280×2772）
- `scripts/damai_mode0_harmony.py` — 模式 0 入口：鸿蒙 MatePad Air（1840×2800）
- `scripts/damai_android_tail.py` — Android 两版共用的确认页后状态协调器
- `scripts/damai_mode0_gui.py`、`scripts/build_damai_mode0_exe.ps1` — EXE 启动器与打包（已 gitignore）
- `requirements-phone.txt` — Android 侧依赖（`uiautomator2` / `uiautodev`）
- `README.md` — 安装与运行
- `PHONE_DEBUG.md` — 真机调试说明（链路、坐标、实测数据、鸿蒙细节）
- `artifacts/` — 运行期日志与结果 JSON（已被 .gitignore 忽略）

## 环境与运行

- Python 环境：`.venv-phone`（仓库根目录，命名不得更改）
- ADB：`E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe`
- hdc：`E:\soft-data\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe`

标准运行命令（三选一，运行前手动把手机停在目标演唱会详情页）：

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_mode0.py          "2026-08-18 13:00:00"
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_K90.py      "2026-08-18 13:00:00"
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_harmony.py  "2026-08-23 21:00:00"
```

鸿蒙版可选参数：`--first-check-after`、`--state-check-interval`、
`--probe-mode auto|grep|recv`、`--terminal-confirm-count`。Android 两版没有可选参数。

## 修改代码时的注意

- 坐标全部以各设备分辨率校准，任何坐标改动必须保留 `EXPECTED_SCREEN_SIZE` 校验；
- 修改盲连点时序、状态闭环或结束条件，视为修改"限制 2"的默认行为，**必须先向用户确认**；
- 结果 JSON 中 `status` 是排查抢票是否走通的关键字段
  （`alipay_reached` / `terminal_no_retry_option` / `timeout` / `timeout_before_start` / `error`）；
- 鸿蒙脚本在改动探测逻辑后，应先跑一次启动自检（`--probe-mode auto` 会打印实际选用的通路），
  确认 grep 通路可用，否则会静默回退 `recv` 拉低探测速度。
