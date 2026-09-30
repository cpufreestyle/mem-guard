# MemGuard 交接文档

> 面向接手维护 / 协作开发的开发者的项目说明。功能与用户向说明见同仓库 `README.md`，本文件聚焦**架构、构建发布、雷区与未结事项**。

## 1. 项目定位（一句话）

Windows 托盘小工具：实时监控**物理内存**与**提交内存(commit)**，超阈值自动清理并提醒，专治「明明还剩几 G 却一直弹『内存不足』」——根因是 commit 耗尽（提交上限 = 物理 + 页面文件），而非物理内存。

- 仓库：`cpufreestyle/mem-guard`（本地 `D:\ai share\repo\mem-guard`，main 分支）
- 最新已发布 tag：`v1.4.3`（CI 自动出 Release，附件 `mem_guard.exe` + `MemGuard-Setup-x.y.z.exe`）
- 开发中（**尚未提交**，勿据本文档判定已发布）：`v1.4.4`——任务栏图标链路：窗口图标 / AUMID 任务栏分组 / 桌面与任务栏快捷方式 / 二次启动唤窗
- 语言/平台：Python 3.8+ / Windows only（清理调用 `NtSetSystemInformation`，无法跨平台）

## 2. 快速上手

```powershell
pip install -r requirements.txt        # psutil / pystray / Pillow
python mem_guard.py                     # 需管理员权限（清理才生效）
python mem_guard.py --once              # 打印一次状态并清理一次
python mem_guard.py --selftest          # 内置自检（CI 与打包后冒烟都用它）
```

- 后台无窗运行：用 `启动 MemGuard（管理员）.bat`（自动提权 + `pythonw`）。
- 配置：首次运行生成同目录 `mem_guard.json`，支持热重载（改完无需重启）。字段见 `README.md` 的「配置文件」表。

## 3. 架构与模块职责

代码已从单文件 `mem_guard.py` 拆为 `memguard` 包。**入口 `mem_guard.py` 是薄启动器**，仅 `from memguard.cli import main`，保持 `python mem_guard.py` 入口不变；PyInstaller 跟随导入把包打进 exe。

| 文件 | 职责 | 关键导出 |
|---|---|---|
| `memguard/config.py` | 版本/路径、默认配置、**配置校验与钳制**、**唯一写盘入口 `update_config`（读-改-写持 `_CONFIG_LOCK`）**、落盘日志 `log` | `__version__`、`load_config`、`save_config`、`update_config`、`normalize_config`、`gb`、`_CONFIG_LOCK`、`CLEAN_BLACKLIST_STEMS`、`_norm_proc_name`、`_blacklist_stems`、`BASE_DIR`、`LOG_PATH` |
| `memguard/winapi.py` | ctypes 绑定：内存读取、进程枚举快路径、特权启用/禁用/查询、单实例互斥体、底层清理调用 | `get_mem`、`process_working_sets`、`is_admin`、`privilege_state`、`enable_privilege`、`disable_privilege`、`_purge_list`、`clear_file_cache`、`single_instance`、`MemoryEmptyWorkingSets/FlushModifiedList/PurgeStandbyList`、`APP_USER_MODEL_ID`、`set_app_user_model_id`、`create_show_event`、`open_show_event`、`close_show_event`、`request_show_overview`、`wait_show_request` |
| `memguard/privileges.py` | **特权管理**：清理所需高特权启用 + 用完即恢复的上下文管理器 | `CLEAN_PRIVILEGES`、`clean_privileges()` |
| `memguard/actions.py` | **清理动作**：单个底层调用的封装（缓存/工作集/修改页/standby/低优先级 standby） | `purge_working_sets`、`flush_modified_list`、`purge_standby_list`、`purge_low_priority_standby`、`clear_system_file_cache`、`empty_process_working_sets` |
| `memguard/clean.py` | **清理编排与统计**：编排各动作、汇总结果；进程 Top 统计（快路径 + 定点补齐 + psutil 兜底） | `do_clean`、`top_processes_list`、`top_processes`、`_run_clean_actions`、`_wait_avail_rise`、`_bump_stats` |
| `memguard/advisor.py` | 优化建议引擎（基于内存状态+配置生成分级建议） | `analyze`、`format_advice` |
| `memguard/ui.py` | 界面层：托盘/窗口图标绘制、气泡、概览/Top10/趋势/建议窗口（不依赖 pystray） | `make_icon`、`app_icon`、`apply_window_icon`、`message_box`、`show_overview_window`、`show_top_window`、`show_trend_window`、`show_advice_window` |
| `memguard/autostart.py` | 开机自启：计划任务注册/查询/卸载（frozen 分支用 XML 导入，兼容含空格路径） | `autostart_enabled`、`install_autostart`、`remove_autostart` |
| `memguard/diag.py` | 诊断导出：状态/进程/日志/配置打包 zip | `export_diagnostics` |
| `memguard/update.py` | 更新检查：GitHub 最新 Release 查询与版本比较（纯标准库 urllib） | `fetch_latest_release`、`_parse_version` |
| `memguard/menu.py` | 托盘菜单：菜单项树构建与全部菜单回调（以 `guard` 为参数，不反向 import tray） | `build_menu(guard)` |
| `memguard/tray.py` | 主循环 `Guard`：状态、配置热重载、监控循环、托盘图标自愈、唤窗信号监听 | `Guard` |
| `memguard/cli.py` | 入口 `main()`、`--once`、`--selftest`、`--show`、控制台隐藏 | `main` |
| `mem_guard.py` | 薄启动器 | — |
| `pin_taskbar.ps1` | 任务栏固定 / 桌面快捷方式（幂等，写 AUMID 与「以管理员身份运行」位） | — |

**依赖方向单向无环**（新接手改代码时务必保持，避免循环导入）：

```
config ← winapi ← privileges ← actions ← clean ← advisor ← ui ← autostart ← diag ← menu ← tray ← cli
                                                          （update 仅依赖 config）
```

## 4. 关键设计点（改代码前必读）

- **特权用完即关**：清理用的 `SeDebugPrivilege` 等高危特权在 `clean_privileges()` 上下文管理器内启用，退出时把「原本禁用」的恢复禁用，不常驻。
- **清理分两档**：保守（只清系统缓存，默认）/ 激进（额外清进程工作集，前台需重读盘可能卡顿）。
- **进程白名单**：`user_blacklist` 在激进档跳过指定进程工作集；内建 `CLEAN_BLACKLIST_STEMS` 永久跳过系统进程。
- **预警 + 防抖**：`warn_margin` 在接近阈值时只提醒不清理；`debounce_sec` 防止边缘抖动误触发。
- **单实例**：命名互斥体，重复启动直接退出。
- **配置热重载**：`Guard.maybe_reload_config` 比对 `mem_guard.json` 的 mtime，变化即重载；越界值被 `normalize_config` 钳制。
- **托盘自愈**：`Guard.run` 监控线程常驻，图标进程异常退出后自动重建，避免「程序在跑但图标没了」。
- **无控制台窗口**：`--noconsole` 子系统；GUI 下 `sys.stdout is None`，日志走 `log()` 写文件而非 `print`。
- **缓存与节流（v1.3.10 起）**：`is_admin`、自启查询 `autostart_enabled`、托盘图标 `make_icon` 均有进程内缓存；托盘菜单展开时**不再**扫描全进程或启动 `schtasks` 子进程（优化建议条数由 `Guard` 在监控线程后台刷新到 `guard.advice_count`，菜单标签只读该缓存）。清理后改为轮询等待可用内存回升（最多 1.5s），日志轮转检查按写入次数节流。改动这些地方请保持"变更后失效 / 后台刷新"的语义。
- **自身资源占用（v1.4.1 起）**：常驻开销的大头是「每个 tick（默认 10 秒）一次的全进程扫描」，另有图标重绘与 HICON 重建，v1.4.1 从四处收敛，改动时请保持同样的口径：
  - `winapi.process_working_sets()` 用 Toolhelp32 快照 + `GetProcessMemoryInfo` 拿全部进程工作集，450 进程实测约 16ms（psutil 逐个建对象约 1150ms）。`clean.top_processes_list()` 走该快路径，**只对句柄被拒（rss=0）的少量 PID** 用 psutil 定点补齐；非管理员下别人会话的进程会成片被拒，故被拒数 >`_FILL_MAX`(24) 时整体跳过补齐（宁可少几行也不能退回秒级）；快路径本身抛异常时回退 `_top_processes_via_psutil`（口径与历史实现一致）。
  - 后台建议刷新：`Guard._refresh_advice` 按 `advice_refresh_sec`（默认 60 秒）做**时间**节流，不再按「每 3 个 tick」计数；且**一次扫描同时喂给 `advisor.analyze(cfg, mem, top)`**，不再各扫一遍。`analyze` 只在没拿到注入 `top` 时才现场扫描，所以菜单/窗口的单次调用仍是实时的。
  - 托盘图标：`Guard._refresh_icon` 只在 `make_icon` 返回的图像对象**身份变化**时才 `icon.icon = ...`（pystray 的 icon setter 每次都 DestroyIcon + 重建 HICON + 发 `NIM_MODIFY`）；鼠标提示照常赋值，因为 pystray 的 `title` setter 自己判等。
  - 图标尺寸与缓存：`ui._icon_size()` 按 `GetSystemMetrics(SM_CXSMICON)` 绘制（夹在 32–64，只测一次），字号按比例缩放；`make_icon` 的进程内缓存上限 128，满了**弹最旧的键**而不是 `clear()` 整体清空。
- **菜单始终读写当前配置（v1.4.2 起）**：`menu.build_menu` 的所有闭包都不缓存配置字典，统一读 `guard.cfg`。热重载（`Guard.maybe_reload_config`）会把 `guard.cfg` **整体替换**成新字典，缓存旧引用有两个后果：菜单勾选态停在过期值；更要紧的是勾选动作会拿旧字典 `save_config`，把用户刚手改的字段整份覆盖回写。热重载同时清零 `_advice_at`，让菜单上的「优化建议（N 条）」立即按新配置重算，而不是最多再等一个 `advice_refresh_sec`。
- **配置只有一个写盘入口 `config.update_config`（v1.4.2 起）**：写配置本质是「读当前文件 - 改 - 写回」，
  菜单勾选、清理统计都会写，不串行就会互相覆盖。`update_config` 全程持 `_CONFIG_LOCK`（可重入，
  `save_config` 也在锁内，嵌套不会死锁），以**磁盘当前内容**为基准做增量合并并返回归一化后的新配置；
  `clean._bump_stats` 同样在锁内重新读盘再改。凡是新增写盘处都走这两个入口，不要再 `save_config(内存字典)`。
- **清理后等待回升会提前收敛（v1.4.2 起）**：`clean._wait_avail_rise` 最多等 1.5s，可用物理一回升立即返回；
  连续 `_RECLAIM_SETTLE`(0.45)s 没回升就收尾——那说明这次清理没释放出可用内存，等满窗口只是让托盘卡住。
  清理成功后 `do_clean` 返回 `stats`，调用方（`Guard._auto_clean` / 菜单手动清理）会同步回 `guard.cfg`，
  否则菜单顶部累计统计要等下一次热重载才刷新。
- **清理区域与多触发源（v1.4.0 起）**：`clean_areas` 控制各清理区域开关（含新增的 `MemoryPurgeLowPriorityStandbyList = 5` 低优先级 standby；旧系统不支持会返回非 0 并如实展示，不算失败）。触发源三种——超阈值（带防抖/冷却）、可用内存低于 `min_avail_mb`（受冷却约束）、定时 `scheduled_minutes`（不受冷却约束），统一走 `Guard._auto_clean`；`clean_on_start` 在监控线程启动时清一次。累计统计 `stats`（count/freed）在 `do_clean` 成功后经 `save_config` 持久化——**每次成功清理都会写一次配置文件**，勿在热重载 mtime 比较上引入写盘循环。
- **左键概览 + 窗口设计约定（v1.4.2 起）**：
  - **左键=主窗口**：pystray 默认左键展开右键菜单，与 WinMemoryCleaner / Mem Reduct（左键开窗、右键给菜单）相反。
    `tray` 自定义 `class _GuardIcon(pystray.Icon)` 覆写 `__call__`，有 `on_left_click` 就走回调（`Guard.open_overview`），
    否则回退 `super().__call__()` 保持右键行为；回调抛异常只记日志不裸崩。
  - **ui 不反向依赖 tray**：概览窗口的按钮用 `hooks` dict 注入（`clean`/`top`/`trend`/`advice`/`history`），
    `Guard.open_overview` 负责把 `do_clean`/`show_*_window`/`self.history` 绑进去。
    **改这里的 import 要靠增量测试兜住**：曾漏 import `show_top_window` 等，`open_overview` 构造 hooks 字典时就 `NameError`，
    三次跳转按钮全废（`tests/test_tray.py::test_open_overview_wires_window_hooks` 是这条的回归）。
  - **进程按名合并**：`ui._group_rows` / `_top_grouped(n)` / `_table_rows` / `_fmt_bytes` 是纯函数（可单测、无 tkinter）。
    `_top_grouped` 先取 `max(n*4, n+10)` 行再合并，避免同名多开把 Top N 名额吃光后凑不满 N 组。
  - **趋势窗首帧空白**：tkinter 布局完成前 `winfo_width/height` 还是 1×1，故 `canvas.bind("<Configure>")` +
    `root.after(100, draw)` 双保险。
  - **概览窗实时刷新（v1.4.2）**：标杆工具的主窗口都是"活"的，故 `_build_overview` 的 `refresh` 加 `with_table`
    开关——`tick()` 每 1000ms 调 `refresh(with_table=False)`（只更进度条/数字，`get_mem` 是廉价 ctypes 调用），
    每 3 个 tick（≈3s）才重建 Top5 表，避免同名进程每帧重排造成视觉抖动；`winfo_exists()` 守卫，窗口关闭即停。
    手动「刷新」与清理后回调用完整 `refresh()`（`with_table=True`）。窗口构建/实时刷新不入 pytest（需真实 tkinter），
    沿用趋势 `tick` 的做法：外部脚本 + 代理 root 手动跑 `after` 队列验证。
  - **三窗无 tkinter 回退**：`show_overview/top/advice_window` 在 `import tkinter` 失败时用 `message_box` 给文本；
    趋势窗无可回退文本，静默返回。测试用 `sys.modules["tkinter"]=None` 触发 + monkeypatch `message_box` 断言。
  - **图标抗锯齿**：`ui._render_icon` 4× 超采样 + LANCZOS 缩回 + 深色描边；`_icon_font(scale)` 按倍数放字号。
    坑：超采样画布的 `textbbox` 已是大坐标系，**不能再乘倍数**，否则数字被画到画布外（`test_icon_text_is_drawn_and_centered` 用像素断言兜住）。

- **任务栏图标链路（v1.4.4）**：要「一枚带 MemGuard 图标的任务栏按钮、点了有反应」，下面四件事必须同时成立——
  - **窗口图标**：`ui.app_icon(size)` / `apply_window_icon(root)` 按 `make_icon.py` 的同一套口径（6/64 边距、radius 12、3 行 4 列存储颗粒）等比重绘并缓存，在概览 / Top10 / 趋势 / 建议四个 `tk.Tk()` 之后各调一次。不挂的话标题栏与任务栏按钮显示的是 Tk（源码运行时甚至是 Python）的默认图标。
    坑：`PhotoImage` 引用必须挂在 root 上（`root._memguard_icon_photos`），Tk 只在命令执行期间持有，被 GC 后任务栏按钮会变空白。
  - **AUMID 分组**：`cli.main` 里 `set_app_user_model_id()`（进程级 `SetCurrentProcessExplicitAppUserModelID`），快捷方式侧由 `pin_taskbar.ps1` 往 `.lnk` 写同一个 `System.AppUserModel.ID`。两边不一致，任务栏会出现「固定的一枚 + 运行中的一枚」两枚按钮；`tests/test_pin.py` 会把两边拉出来对账，改一边必须改另一边。
  - **快捷方式**：Windows 11 已从 `.lnk` 右键菜单移除「固定到任务栏」（只剩「固定到开始」），只能走兜底——把快捷方式放进任务栏固定目录 `\APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar`，资源管理器会自动显示成任务栏按钮。
    `.lnk` 的「以管理员身份运行」是头里 LinkFlags（偏移 20）的 `SLDF_RUNAS_USER`(0x2000) 位，老的兼容性选项 / verb 在**新版资源管理器上已不生效**，直接翻位最稳；且必须在写 AUMID **之后**翻（`IPersistFile.Commit` 会整存一遍 `.lnk`，可能把先翻好的位冲掉）。
  - **二次启动唤窗**：主实例 `create_show_event()` 建命名事件（`Local\MemGuard_ShowRequest`），`Guard._show_waiter` 守护线程轮询 `wait_show_request`；新进程在单实例分支 `request_show_overview()` 发信号后 `sys.exit(0)`——静默退场，不再弹「已在运行」提示框。
    开机自启不带 `--show`，登录时不会弹窗；`show_on_start` 是**一次性开关**（托盘自愈重建图标后不重复弹窗）。

## 5. 构建与发布

**本地打包**（产物 `dist\mem_guard.exe`，约 18MB）：

```powershell
pip install pyinstaller
.\build.ps1                 # 默认单文件；结束自动跑 --selftest 冒烟，失败即报错
.\build.ps1 -OneDir         # 目录版（启动快、单进程）
.\build.ps1 -NoTk           # 排除 tkinter（体积小约 10MB，趋势窗口不可用）
.\build.ps1 -Upx            # 启用 UPX 压缩（默认关：易被杀软误报）
```

**本地编译安装器（可选）**：需自装 Inno Setup 6，然后执行
`ISCC.exe /DAppVersion=<版本> installer\mem_guard.iss`，产物为 `dist\MemGuard-Setup-<版本>.exe`。

**自动发布（GitHub Actions）**：`.github/workflows/release.yml` 为 `on: push: tags: ["v*"]`。流程：安装依赖 → `pytest -q` → `--selftest` → PyInstaller 打包 → 冻结版 exe 跑 `--selftest` → **Inno Setup 编译安装器**（`installer/mem_guard.iss`，版本号从 `config.py` 注入）→ 上传产物 → 创建 Release（附件 `mem_guard.exe` + `MemGuard-Setup-x.y.z.exe`）。

```powershell
# 发版三步
# 1) 先把 __version__（config.py）与 README 的「当前版本」手动 +1
git add -A
git commit -m "refactor/fix: ..."
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z       # 推 tag 即触发 CI 出 Release
```

### ⚠ 发布相关雷区（已踩过，必看）

- **确认发布状态别用 GitHub API**：匿名 API 限流 60 次/小时，会误判成「404/失败」。改用 `releases.atom` 订阅源或 Releases 网页确认最稳。注意 atom 也可能因本机到 `github.com:443` 的连接超时而**假阴性**（`curl -s` 会把错误吞掉），此时用 `api.github.com` 的 `releases/latest` 与 `actions/runs` 交叉确认，别据单次失败判定发布失败。
- **CI 中文乱码**：GitHub Windows runner 控制台非 UTF-8，Python 打中文会 `UnicodeEncodeError` 致自检失败。CI 已设 `PYTHONUTF8: 1` + `PYTHONIOENCODING: utf-8`，`cli.py` 入口也有 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` 兜底——新增打印中文处别破坏这个。
- **GUI 子系统 exe 退出码**：PowerShell 7（CI runner 默认）下 `& exe; $LASTEXITCODE` 不可靠，必须用 `Start-Process -Wait -PassThru` 取 `.ExitCode`（见 `release.yml` / `build.ps1`）。
- **runner 偶发卡顿**：个别 tag 构建耗时可达 3 分钟（正常约 1 分钟），是排队变慢不是失败，等一会儿再确认。

## 6. 测试现状

- **单元测试（v1.3.11 起）**：`tests/` 下 pytest 用例（134 项，11 个文件）覆盖 `config` 归一化/钳制、`update._parse_version`、`advisor.analyze`（注入内存状态与 `top` 快照，不依赖真实机器）、`ui` 图标取色/尺寸/缓存淘汰（含"颜色须按原始 float 判断"的回归）、`clean` 的快路径排序/定点补齐/超限不补齐/psutil 兜底、`winapi.process_working_sets` 真实进程对账、`tray` 的图标去重与建议时间节流、`clean`/`actions` 常量绑定回归（拦截底层调用，防 `NameError` 类回归）、`autostart` 缓存语义、`menu` 热重载后仍读写当前配置（防写回覆盖手改值）、`tray` 热重载清零建议节流、`update_config` 增量合并/不覆盖并发写、`_wait_avail_rise` 提前收敛与 `_bump_stats` 口径、`do_clean` **管理员路径**（mock 特权与各动作：档位/区域开关/成败判定/统计）、`winapi` 读取与缓存。v1.4.2 又补「美观/实用性」三项：`ui` 概览与表格纯函数（`_group_rows` 合并同名/`_top_grouped` 超采样再合并/`_fmt_bytes` 单位/`_table_rows` 的 ×N 标记与空 total 占位）、`show_overview/top/advice_window` 的**无 tkinter 回退**（`sys.modules["tkinter"]=None` 触发 ImportError + monkeypatch `message_box` 断言）、以及左键链路`_GuardIcon.__call__`→`open_overview` 与 hooks 绑定回归。运行：`pip install -r requirements-dev.txt && python -m pytest -q`。
- **任务栏链路回归（v1.4.4 新增）**：`tests/test_pin.py`（8 项：AUMID 与 `cli.py` 侧对账、`SLDF_RUNAS_USER` 位翻/清、任务栏固定目录路径拼接）；`tests/test_cli.py`（4 项：`--show` 早退派发）；`tests/test_winapi.py`（17 项，含 `set_app_user_model_id` 与命名事件 `Local\MemGuard_ShowRequest` 的建/等/发）。注意：monkeypatch 唤醒事件常量时务必核对 `winapi.SHOW_EVENT_NAME`，写成 `SHOWOW_EVENT_NAME` 会让唤窗用例假绿。全量合计 144 项（11 个文件；v1.4.4 新增 test_cli.py 4 项、test_pin.py 8 项；收尾再加 test_winapi 类图标 7 项、test_ui 类图标覆盖 3 项）。
- **内置自检**：`python mem_guard.py --selftest` 覆盖 get_mem / make_icon / 配置钳制 / 黑名单匹配 / `_parse_version` / advisor / `build_menu` 等，全 PASS 才说明导入链与基本逻辑 OK。非管理员环境下 `do_clean` 走「需管理员」早返回分支，不会真正清理。
- **CI 顺序**：安装 `requirements-dev.txt` → `pytest -q` → `--selftest` → PyInstaller 打包 → 对**冻结版 exe** 再跑一次 `--selftest` 冒烟。

## 7. 已知问题 / 风险

- **杀软误报（未根治）**：因调用 `NtSetSystemInformation`，行为类似 ISLC/Mem Reduct，易被启发式误报 / SmartScreen 拦截。**代码签名用户已明确搁置**（2026-09-16），目前只能加白名单缓解。若日后要做，需提供证书或接入付费签名服务。
- **非管理员清理无效**：自动清理在非管理员下只记录不执行（这是设计，不是 bug）。
- **历史 bug 已修（v1.3.9 正式修复）**：`clean.py` 曾漏导入 `MemoryEmptyWorkingSets/FlushModifiedList/PurgeStandbyList` 三个常量，非管理员路径永不触发所以 `--selftest` 发现不了，**管理员下清理会 `NameError` 失效**。v1.3.9 由 `actions.py` 显式导入修复，已发布。
- **自启查询解码异常（v1.3.9 修复）**：`autostart._run_silent` 用 `subprocess.run(text=True)` 且未给 `errors`，中文系统下 `schtasks` 输出 GBK 会在 subprocess reader **线程**内抛 `UnicodeDecodeError`（主线程的 `except` 抓不到，只在日志留 traceback）。已加 `errors="replace"`；该函数只用 `returncode`，不解析输出。

## 8. 待办 / 接手清单

1. **~~收尾 v1.3.9~~（已完成 2026-09-18）**：已新增 `privileges.py`/`actions.py`、`clean.py` 重写、`NameError` 修复、自检新增 `build_menu` 用例，README 模块表与依赖链已同步；本地 `--selftest` 全 PASS，已 commit `50b8fd1` 并 tag `v1.3.9` 推送（CI 自动发布 Release）。
2. **~~补 pytest 单元测试~~（已完成 2026-09-18，v1.3.11）**：已加 `tests/`（51 项）与 `requirements-dev.txt`，CI 在打包前跑 `python -m pytest -q`；`clean`/`actions` 常量绑定回归、`ui` 取色 float 回归等均已覆盖。
3. **~~安装器~~（已完成 2026-09-18，v1.3.12）**：采用 Inno Setup（`installer/mem_guard.iss`，**当前用户安装** `{localappdata}\Programs\MemGuard`，安装免 UAC，配置/日志可正常写入）；CI 用 `choco install innosetup` 编译、版本号从 `config.py` 注入，Release 同时附带 `MemGuard-Setup-x.y.z.exe`。注意：脚本已探测 `ISCC.exe` 常见路径，找不到会**告警并跳过**而非让发布失败。
4. **~~`do_clean` 拆分（v1.4.2 完成）~~**：动作编排抽到 `clean._run_clean_actions`（特权窗口内只做动作，返回 detail/core/failed_privs），`do_clean` 只剩「定档位 → 采样 → 编排 → 判成败 → 等回升 → 统计 → 日志」。顺手补齐了明细一致性：`modified`/`standby` 关闭时原来不打印任何行，现在和其它区域一样显示「已关闭」。**注意**：管理员路径原先完全没测试（v1.3.9 的 `NameError` 正是从这条路径漏掉的），现已由 `tests/test_clean.py` 的 `_admin_env` mock 覆盖——改这段务必跑 `python -m pytest -q`。
5. **~~配置读写竞态（v1.4.2 修，2026-09-23，尚未打 tag）~~**：三个问题一次收干净——
   - 菜单闭包原先捕获 `cfg = guard.cfg`，热重载会整体替换字典 → 勾选态读到过期值，且勾选动作把旧配置整份写回，覆盖用户刚手改的值。现已统一读 `guard.cfg`；
   - 写盘没有统一入口且不加锁：菜单勾选 vs 清理统计（`save_config`）两个「读-改-写」并发时会互相覆盖。现全部收敛到 `config.update_config` / `clean._bump_stats`，在 `_CONFIG_LOCK` 内以磁盘当前内容为基准做增量合并（实测旧写法会把并发的 `auto_clean` 回滚，新写法保住）；
   - 清理后的统计只落盘没同步内存，菜单顶部累计统计要等下次热重载（`interval` 最长 1 小时）才刷新。现已由调用方同步回 `guard.cfg`。
   回归见 `tests/test_menu.py` / `tests/test_config.py` / `tests/test_clean.py`。发布仍走 §5 三步。
6. **已评估并否决的「优化」（附实测，别再重复踩）**：
   - `empty_process_working_sets` 改用 Toolhelp32 枚举：本机实测快照 8ms vs `psutil.process_iter(["pid","name"])` 2ms（Windows 上 psutil 走单次 `NtQuerySystemInformation`，比 Process32NextW 逐个拷 260 字符结构体更快），**已回退**。v1.4.1 里 Toolhelp32 快路径的收益来自顺带拿到工作集（省去逐进程查询），不是枚举本身。
   - `advisor` 与菜单 Top10 共享一份 Top 扫描：两者触发时机不同（后台 60s vs 人点窗口），共享只会带来过期数据，没有重复开销可省；单次扫描实测约 2ms，不值得为它引入缓存失效逻辑。
   - `log()` 常驻文件句柄 + 写锁：日志每 tick 才几行，且轮转 `os.replace` 与常驻句柄在 Windows 上互相打架（需先关句柄再轮转），收益不抵复杂度。
7. **~~v1.4.1 资源占用优化~~（已完成并发布 2026-09-20）**：见 §4「自身资源占用」。commit `e1d54f7`、tag `v1.4.1` 已推送，CI run #15 success，Release 附件 `mem_guard.exe` + `MemGuard-Setup-1.4.1.exe`。180 秒空载实测累计 CPU 3.547s → 0.047s。遗留可选项（评估结论见第 6 条）：`advisor` 里「高内存占用进程」与菜单 Top10 仍各自扫描（都是人触发的低频路径，不影响常驻开销）；Pillow 仍在依赖里（`make_icon` 用它绘图，去掉要自写位图渲染，收益不抵风险）。
8. **v1.4.4 任务栏图标链路（代码已完成，尚未 commit/tag）**：四件事必须同时成立——窗口图标（`ui.app_icon` / `apply_window_icon`）、AUMID（`cli.set_app_user_model_id` 与 `.lnk` 的 `System.AppUserModel.ID` 一致）、快捷方式（`pin_taskbar.ps1`）、二次启动醒窗（命名事件 + `Guard._show_waiter`）。
   - 用法：`powershell -NoProfile -ExecutionPolicy Bypass -File .\pin_taskbar.ps1`（幂等，可重复跑；写 AUMID + 翻「以管理员身份运行」位 + 往任务栏固定目录拷 `.lnk`）。首次点击桌面快捷方式会弹一次 UAC（设计如此，不是 bug）。
   - **AUMID 两边必须一致**：进程侧 `SetCurrentProcessExplicitAppUserModelID` 与 `.lnk` 的 `System.AppUserModel.ID` 不一致时，任务栏会出现「固定的一枚 + 运行中的一枚」两枚按钮；改任一侧必须同步另一侧，`tests/test_pin.py` 已把两边拉出来对账。
   - **取证结论（2026-09-30，真实 exe 实测，非源码推演）**：
     - 打包：`dist/mem_guard.exe` = 20,371,797 字节（v1.4.4 修复 SMALL(16) 前为 20,366,409；v1.4.3 旧版 20,368,319 已备份 `%TEMP%\mem_guard_v143_backup.exe`）。坑：`Start-Process -ArgumentList` 传 `-File` 路径含空格必须整体加引号，否则 pyinstaller 静默不产物——构建后先 `Test-Path dist\mem_guard.exe` 再谈冒烟。
     - 取窗口：`root.winfo_id()` 是 `TkChild`（类图标 0），真窗口必须 `EnumWindows` + `GetClassNameW == "TkTopLevel"`；实测 GCLP_HICON=32bpp 32×32、GCLP_HICONSM=16×16，WM_GETICON=0。窗口级 AUMID `vt=0` 是预期（AUMID 走进程级 `SetCurrentProcessExplicitAppUserModelID`）。onefile exe 的真实宿主是**子进程**，launcher pid ≠ 窗口 pid，`find_window` 只能按标题匹配。**坑**：`GetParent(root.winfo_id())` 在建窗后、任何 update 之前返回 0——`TkTopLevel` 包装窗口要 `update_idletasks` 才创建；`apply_window_icon` 因此先 `update_idletasks` 拿句柄再覆写类图标（实测 update/deiconify 之后类图标句柄不变，map 不会把覆写洗掉）。
     - exe 内嵌图标 = `mem_guard.ico` 5 帧（16/24/32/48/64）逐字节一致、逐像素 diff=0。Tk 写 HICON 是**预乘 alpha**，比对前必须反预乘再比。
     - BIG(32) 类图标与 `app_icon(32)` 比：475 对 fully-opaque 像素 max_delta=0（diff_ratio 里那 52 个 beyond-tol 全是低 alpha 边缘 DrawIconEx 画到零底 DIB 的混色噪声，旧版同样存在；任务栏按钮由渲染器自己画，不受影响）。
     - SMALL(16) 类图标曾因 Tk 自行下采样 + 预乘 alpha 往返偏暗（beyond-tol=151，medRGB rt=(70,122,236) vs ref=(89,136,238)），**v1.4.4 收尾已修**：`winapi.icon_handle` 用 BGRA XOR + 全零 AND（无预乘）自造 16px HICON，`ui._override_class_icons` 在 iconphoto 后覆写 `GCLP_HICONSM`。直播复验（新 exe 20,371,797 字节）：100 对不透明像素 max_delta=0、beyond-tol=0、medRGB=(89,136,238) 与 ref 完全一致（修复前 max_delta=43、48 对偏暗）。
     - 二次启动醒窗：先 `ShowWindow(hw,6)` + `IsIconic` 确认最小化 → 第二实例 1.8s 内 exit=0 → 窗口 `IsIconic=False` 且可见，**含最小化还原验证通过**。`foreground=False` 是测量假象（取证会话自身持有前台），非缺陷。
     - 快捷方式：`pin_taskbar.ps1 -Where both` 已幂等刷新桌面 `D:\Desktop\MemGuard.lnk` 与任务栏固定目录各一枚（目标 dist exe、`--show`、图标 `exe,0`、AUMID `MemGuard.MemoryGuard`、SLDF_RUNAS_USER 位）。桌面上另有旧版 `MemGuard 内存守护.lnk`（powershell 隐藏窗 + RunAs 包装，每次点击都弹 UAC 且无 AUMID），已建议用户删除。
     - 测试：`python -X utf8 -m pytest tests -q` 全量 144 项通过。
   - 收尾顺序：任务栏按钮**实际外观**仍只能由用户在本机 console 会话目视（RDP `CopyFromScreen` 全黑抓不到），本机取证目前覆盖到「类图标像素一致 + AUMID 一致 + .lnk 参数正确 + 醒窗还原」；用户确认后**逐个征求同意**才 commit/tag/push。

## 9. 其他踩坑索引（详见 `.codebuddy/memory` 的 `MEMORY.md`）

- 含中文的 `.ps1` 必须 **UTF-8 with BOM**，否则 PowerShell 5.1 按 GBK 解中文引号破坏语法。
- `git commit -m` 信息里**不要写 `%TEMP%` 这类 `%VAR%`**，会触发「文件名语法不正确」导致整条命令（含 `git add`）失败。
- 后台化 + 输出重定向到工作区文件 = **撑爆磁盘风险**（实测 33GB 直到 C 盘 0 字节）：构建日志写 `$env:TEMP` 并加 `try/catch` 兜底。
- **`schtasks` 注册含空格路径会拆 Command/Arguments**：`schtasks /TR "D:\ai share\...\mem_guard.exe"` 会把路径按第一个空格拆成
  `Command=D:\ai` + `Arguments=share\...\mem_guard.exe`，写入的计划任务开机根本起不来。**解法**：改用 XML 导入
  （`schtasks /Create /TN MemGuard /XML <file> /F`，XML 里 `<Command>` 放完整路径、不带 `<Arguments>`），`RunLevel=HighestAvailable`
  登录时静默提权、逐次无 UAC。另：在**非管理员** shell 里建 `HIGHEST` 计划任务会 `Access is denied`，需一次 UAC 提权
  （`Start-Process schtasks ... -Verb RunAs`）；`autostart._install_autostart_impl` 冻结版就是注册 `sys.executable` 自身。
- **`Start-Process -ArgumentList` 不带引号必翻车**：`-File` 与路径分开传、路径又没整体加引号时，含空格路径会被拆成多个参数，pyinstaller 拿不到脚本、静默不产物（build.ps1 前两次失败即此）；必须写成 `-ArgumentList "-File D:\ai share\...\build.ps1"` 这种整体加引号的形式。判别方法：构建后先 `Test-Path dist\mem_guard.exe`（v1.4.4 应为 20,371,797 字节）再跑 `--selftest` 冒烟。
- **桌面快捷方式**：`WshShortcut` 无法直接设「以管理员运行」，故快捷方式直接指向 exe；要双击即提权需用户在 .lnk 属性里勾选，
  或靠托盘菜单「开机自启」的计划任务（HighestAvailable）实现静默提权自启。给提示气泡归因正常，避免用 powershell/cmd 壳进程当中转。
- **Win11 右键已无「固定到任务栏」verb**：Shell 里只剩「固定到开始」，`.lnk` 的 `Verbs()` 拿不到；兜底是把快捷方式直接拷进任务栏固定目录 `%APPDATA%\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar`，资源管理器会自动显示成任务栏按钮。
  `.lnk` 的「以管理员身份运行」在老的兼容性选项 / verb 上已不生效，只能直接翻文件头 LinkFlags（偏移 20）的 `SLDF_RUNAS_USER`(0x2000) 位；且必须在写 AUMID **之后**翻——`IPersistFile.Commit` 会整存一遍 `.lnk`，可能把先翻好的位冲掉。
- **RDP 会话里 `Graphics.CopyFromScreen` 抓不到任务栏**：截出来全黑，是会话限制不是 bug。要验证任务栏按钮只能让用户在本机 console 会话目视（必要时注销重登）；替代办法是渲染对比图——把 `ui.app_icon` 的输出与 `make_icon.py` / `mem_guard.ico` 并排像素比对。
- **`apply_patch` 往 `.py` 插新行容易把缩进弄丢**：新增行若只带 LF 行尾、与文件里既有的 CRLF 混用，按行读回来时多个逻辑行会被拼成一整行（例如 `tray.py` 里 7 空格缩进的函数体塌成一行）。凡是给 `.py` 插行，插完立刻 `python -X utf8 -m pyflakes <file>` + 查 lone LF。

---
最后更新：2026-09-30（v1.4.4 任务栏图标链路：`ui.app_icon` / `apply_window_icon` 按 `make_icon.py` 口径给四个 tkinter 窗口挂图标（`PhotoImage` 必须挂在 root 上防被 GC）；`cli.main` 设进程级 AUMID + `pin_taskbar.ps1` 给 `.lnk` 写同一个 `System.AppUserModel.ID`，并翻文件头 LinkFlags（偏移 20）的 `SLDF_RUNAS_USER` 位；命名事件 `Local\MemGuard_ShowRequest` + `Guard._show_waiter` 让二次启动静默醒窗。Win11 已移除「固定到任务栏」verb，只能拷 `.lnk` 进固定目录。收尾新增 `winapi` 窗口类图标区块（`icon_handle` 自造 32/16 HICON 覆写 `GCLP_HICON`/`GCLP_HICONSM`，修 Tk 小图标发灰；`ui._override_class_icons` 先 `update_idletasks` 催出 `TkTopLevel` 再覆盖）。补 `tests/test_pin.py`(8) / `tests/test_cli.py`(4) / `tests/test_winapi.py` 醒窗与窗口类图标用例，全量 144 项全绿，`pyflakes` 干净，`--selftest` 全 PASS，exe 已重打包并经真实 exe 取证（内嵌图标与 `mem_guard.ico` 逐帧一致、BIG(32)/SMALL(16) 类图标像素级一致（475/100 对不透明像素 max_delta=0）、二次启动醒窗含最小化还原）。桌面与任务栏快捷方式已用 `pin_taskbar.ps1 -Where both` 幂等刷新。**尚未 commit/tag/push，待用户目视确认任务栏按钮外观后逐个征求同意再发版**。上一节点见下）
历史：2026-09-27（v1.4.3：修 `autostart.py` frozen 分支注册「开机自启」的方式—`schtasks /TR` 会把含空格路径（本机仓库就在 `D:\ai share\...`，打包后 exe 同病）按第一个空格拆成 `Command=D:\ai` + `Arguments=share\...`，登录时任务根本起不来；改为写临时 XML 走 `schtasks /Create /XML <file> /F` 导入。新增 `_task_xml`：`<Command>` 放完整路径、不带 `<Arguments>`，转义 `&<>`，`LogonTrigger` + `RunLevel=HighestAvailable` 与 `install_autostart.ps1` 口径一致。补两条回归测试（完整路径落在同一个 `<Command>` / XML 特殊字符转义），共 110 项全绿，`--selftest` 全 PASS。本机已落地桌面快捷方式「MemGuard 内存守护.lnk」+ 登录自启计划任务；README 补绿色版搬家/重打包后需重新勾选自启的说明。已 commit(`672ec5b`)并 tag/push v1.4.3：CI run #17 success，Release 附件 `mem_guard.exe`(18.4MB) + `MemGuard-Setup-1.4.3.exe`(20.5MB) 已就位。上一节点见下）
历史：2026-09-26（v1.4.2：在「实用/美观」方向对标 WinMemoryCleaner / Mem Reduct——**左键点托盘打开内存概览主窗口**（`_GuardIcon` 覆写 `__call__` + `open_overview` 注入 hooks），概览窗带进度条 + Top5（同名进程合并 ×N）+ 快捷按钮；Top10/趋势/建议窗改 ttk 表格与斑马纹、趋势图加坐标轴与当前值标注、修趋势窗首帧空白；托盘图标改 4× 超采样抗锯齿。**修了一个漏 import 的 `NameError`**（`open_overview` 引用未导入的 `show_*_window`，三个跳转按钮全废）。补齐概览/表格纯函数、无 tkinter 回退、左键链路三类测试，概览窗再加每秒实时刷新（进度条每 1s、Top5 表每 3s）；三类纯函数/回退/链路测试补齐后共 108 项全绿，`pyflakes` 干净，`--selftest` 与 PyInstaller 冻结版冒烟全 PASS。已 commit(`a5a5352`)并 tag/push v1.4.2（CI run #16 success，已确认）。）
历史：2026-09-24（v1.4.2 部分：菜单统一读 `guard.cfg`；新增唯一写盘入口 `update_config`（持锁增量合并）消灭配置写竞态；清理后等待回升提前收敛、统计同步回内存；`do_clean` 拆分出 `_run_clean_actions` 并补齐管理员路径测试（92 项），PyInstaller 冻结版冒烟通过）
历史：2026-09-20（对应 v1.4.1：自身资源占用优化——进程扫描快路径、后台建议按时间节流并复用快照、托盘图标按需重绘且按系统指标尺寸绘制；180 秒空载 CPU 由单核 1.97% 降到 0.026%，已发布）
