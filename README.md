# 大麦抢票 · 模式 0

三台真机上的「模式 0」抢票自动化：**前置连点 + 状态驱动提交**。

程序只执行一条链路：

```text
演唱会详情页 → 票档处理 → 确认购买 → 立即提交 → 支付宝收银台
```

检测到支付宝成为前台 App 后**立即退出**，不校验金额，**绝不点击支付宝"付款"**。

模式 0 不读取任何 profile 配置文件，也不需要 `--selection-mode` 之类的参数：
命令行只传一个**本地开售时间**，其余全部硬编码在脚本顶部的常量里。

## 安全红线（永不可违反）

- 绝不点击支付宝的"付款 / 确认支付"按钮，程序不校验金额；
- 程序不返回、不搜索、不整理页面，每次执行前由操作者手动把手机停在目标演唱会详情页；
- 永不点击"返回重新选购"按钮（它是终态弹窗的唯一出口，点它等于放弃抢票）。

## 三台设备

| # | 设备 | 脚本 | 校准分辨率 | 通信工具 |
| --- | --- | --- | --- | --- |
| 1 | `8595251f`（Android 主力机） | `scripts/damai_mode0.py` | `1440×3200` | adb |
| 2 | `f109b923`（REDMI K90 Ultra） | `scripts/damai_mode0_K90.py` | `1280×2772` | adb |
| 3 | HUAWEI MatePad Air（HarmonyOS） | `scripts/damai_mode0_harmony.py` | `1840×2800` | hdc |

启动时会强校验屏幕尺寸，与脚本常量不一致**直接报错退出**——这是所有坐标安全的前提。

## 环境要求

- Python 虚拟环境**必须命名为 `.venv-phone`**（位于项目根目录，隐藏目录带点前缀）。
  后续命令统一指向 `.venv-phone\Scripts\python.exe`，命名不一致会导致脚本无法运行，
  请勿改名、勿新建其他名字的环境。
- ADB：`E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe`
- hdc（仅鸿蒙设备需要）：`E:\soft-data\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe`

创建环境（仅首次）：

```powershell
python -m venv .venv-phone
.\.venv-phone\Scripts\python.exe -m pip install -r requirements-phone.txt
```

> 鸿蒙脚本只用 Python 标准库 + hdc，不需要上面的依赖；
> `uiautomator2` / `uiautodev` 仅 Android 两个脚本需要。

## 运行

运行前请手动把手机停在大麦目标演唱会详情页，然后传入本地开售时间（`YYYY-MM-DD HH:MM:SS`）。

**① Android 主力机 `8595251f`：**

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_mode0.py "2026-08-18 13:00:00"
```

**② REDMI K90 至尊版 `f109b923`：**

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_K90.py "2026-08-18 13:00:00"
```

**③ 鸿蒙 MatePad Air：**

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_mode0_harmony.py "2026-08-23 21:00:00"
```

鸿蒙版支持以下可选参数（Android 两版没有）：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--first-check-after <秒>` | `2` | 首次页面探测的时点（开售后 N 秒） |
| `--state-check-interval <秒>` | `0.5` | 点击暂停时的周期探测兜底 |
| `--probe-mode auto\|grep\|recv` | `auto` | 探测通路；自检失败自动回退 `recv` |
| `--terminal-confirm-count <N>` | `2` | 终态弹窗需连续确认的次数 |

## 运行结果

脚本结束时在 stdout 打印结果 JSON，关键字段：

| 字段 | 含义 |
| --- | --- |
| `status` | `alipay_reached` / `terminal_no_retry_option` / `timeout` / `timeout_before_start` / `error` |
| `main_tap_count` / `retry_tap_count` | 主操作点 / 「继续尝试」点击次数 |
| `first_tap_wall_time` | 首次点击的墙上时间（毫秒精度） |
| `retry_dialog_hits` / `terminal_dialog_hits` | 弹窗命中次数 |
| `seconds_after_sale` | 相对开售时刻的耗时 |
| `main_action_point` / `retry_dialog_point` | 本次实际使用的坐标（核对设备用） |

`status` 为 `alipay_reached` 表示已到收银台，抢票链路成功走完。

## 可选：8595251f 的 GUI / EXE 版

`scripts/damai_mode0_gui.py` 是 tkinter 启动器（EXE 入口）：输入开售时间后直接调用
`damai_mode0.run(sale_time)`，**不改动任何模式 0 逻辑**；只覆盖 `8595251f`。

打包脚本为 `scripts/build_damai_mode0_exe.ps1`，产物为 `dist\damai_mode0.exe`。

> 这两者属于 EXE 构建相关代码，与 `dist/`、`build/`、`*.spec` 一样**已在 `.gitignore` 中忽略**，
> 不会随仓库上传；本地仍保留，可随时重新打包。

## 目录结构

```text
scripts/
├── damai_mode0.py             # 模式 0 入口：Android 8595251f（1440×3200）
├── damai_mode0_K90.py         # 模式 0 入口：REDMI K90 至尊版（1280×2772）
├── damai_mode0_harmony.py     # 模式 0 入口：鸿蒙 MatePad Air（1840×2800）
├── damai_android_tail.py      # 两个 Android 入口共用的确认页后状态机
│                              #（运行期依赖，删除后 Android 两版无法启动）
├── damai_mode0_gui.py         # 【已 gitignore】EXE 的 GUI 入口
└── build_damai_mode0_exe.ps1  # 【已 gitignore】EXE 打包脚本
requirements-phone.txt         # Android 侧依赖（uiautomator2 / uiautodev）
PHONE_DEBUG.md                 # 真机调试说明：链路、坐标、实测数据、鸿蒙细节
AGENTS.md                      # 本仓库对 AI 的硬性限制
```

## 更多细节

运行链路、坐标校准依据、性能实测数据、鸿蒙平台差异与息屏注意事项，见 `PHONE_DEBUG.md`。
