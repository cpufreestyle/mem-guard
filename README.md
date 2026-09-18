# MemGuard — Windows 内存守护托盘工具

实时监控**物理内存**与**提交内存(commit)**，超阈值自动清理并提醒的 Windows 托盘小工具。

## 为什么需要它

Windows 弹「内存不足 / 虚拟内存不足」时，真正耗尽的是**提交内存（commit）**，而不是物理内存。
任务管理器默认不显示 commit，于是经常出现"明明还剩几个 G 却一直弹内存不足"的怪现象。
MemGuard 同时监控这两个指标，并把 commit 放在最显眼的位置。

## 功能

- 托盘图标实时显示物理内存使用率，颜色随压力变化（绿 → 黄 → 红，分档跟随可调阈值）
- 悬停显示物理内存 + 提交内存详情
- 任一指标超阈值时自动清理并弹气泡提醒；接近阈值（`warn_margin`）时先弹**预警**，只提醒不清理
- 清理动作**分两档**（原理同 ISLC / Mem Reduct，调用 `NtSetSystemInformation`）：
  - **保守档（默认）**：1) 刷写修改页列表（FlushModifiedList）2) 清理 standby list（PurgeStandbyList）
    3) 清空系统文件缓存工作集 —— 温和，对前台程序几乎无影响
  - **激进档**：在保守档基础上再清空各进程工作集（EmptyWorkingSet）—— 释放更多，但前台程序下次访问需重新读盘
- **进程白名单**（`user_blacklist`）：指定进程在激进档下跳过工作集清空，避免浏览器 / IDE / 游戏被清后卡顿
- 右键菜单：立即清理 / 内存占用 Top10（可刷新窗口）/ 自动清理开关 / 清理阈值（激进·标准·宽松）/ 清理力度（保守·激进）/ 清理冷却（1/5/10/30 分钟）/ 开机自启开关 / 打开日志 / 退出
- 日志自动轮转（超过 1MB 归档为 `mem_guard.log.1`）
- 单实例运行（命名互斥体）
- 清理所需特权**用完即关**（不常驻 `SeDebugPrivilege` 等高危特权）
- **后台静默运行**：全程无控制台窗口（用 `pythonw.exe` 启动，程序内也会主动隐藏控制台）
- 配置热重载（改 `mem_guard.json` 无需重启）、内存趋势窗口、一键导出诊断
- 超阈值自动清理支持**防抖**（`debounce_sec`），避免内存边缘抖动误触发
- **配置校验**：越界 / 脏配置会被自动钳制回合法范围，不会让程序跑飞
- **托盘自愈**：托盘后端异常退出（如 Explorer 重启）后自动重建图标，避免「程序在跑但图标不见了」
- **检查更新**：托盘菜单可查询 GitHub 最新 Release，发现新版本会自动打开下载页
- **优化建议**：托盘菜单「优化建议」一键分析当前内存状态与配置，给出分级可操作项（页面文件/虚拟内存、权限、阈值合理性、进程高占用与白名单、健康态），无 tkinter 时回退为气泡文本

## 环境要求

- Windows 10 / 11
- Python 3.8+
- 依赖：`pip install -r requirements.txt`

## 安装与运行

```powershell
pip install -r requirements.txt
# 以管理员身份运行（清理需要管理员权限）
python mem_guard.py
```

或直接双击 `启动 MemGuard（管理员）.bat`（会自动请求管理员权限，并以后台无窗口方式启动）。

> 提示：托盘程序请用 `pythonw.exe` 启动（本 bat 已自动优先使用），这样不会出现控制台黑框；
> 若用 `python.exe` 启动，程序也会在启动瞬间自动隐藏控制台窗口。

命令行辅助模式：

```powershell
python mem_guard.py --once       # 打印一次内存状态并执行一次清理
python mem_guard.py --selftest   # 运行内置自检
python -m pytest -q              # 运行单元测试（先 pip install -r requirements-dev.txt）
```

## 打包为独立 exe（免 Python、跨机器分发）

本工具清理内存依赖 Windows 专有 API（`NtSetSystemInformation` / Win32），
因此**可执行文件只能在 Windows 上运行**，无法编译成真正跨平台的版本。
但可打包成单个 `.exe`，目标机器无需安装 Python 与依赖，拷过去即可后台运行：

```powershell
pip install -r requirements.txt   # 运行时依赖（psutil / pystray / Pillow）
pip install pyinstaller           # 打包工具（requirements.txt 不含）
.\build.ps1                       # 生成 dist\mem_guard.exe（GUI 子系统，无控制台黑框）
```

构建选项：

| 参数 | 产物 | 说明 |
| --- | --- | --- |
| 默认 | `dist\mem_guard.exe` | 单文件（约 30MB），便于跨机器拷贝分发 |
| `-OneDir` | `dist\mem_guard\mem_guard.exe` | 目录版：启动更快、进程列表只有 1 个进程；分发需整目录拷贝 |
| `-NoTk` | 体积再小约 10MB | 排除 tkinter/tcl-tk；代价：「内存趋势」窗口不可用，「Top10」回退为 MessageBox |

体积优化（可选）：把 [UPX](https://upx.github.io/) 的 `upx.exe` 放到 `tools\upx-*\`（或加入 PATH），
再用 `.\build.ps1 -Upx` 启用压缩即可。
**默认不启用**：经 UPX 压缩的 exe 更容易被杀软启发式规则误报。

脚本已默认排除 `numpy`/`pandas`/`matplotlib`/`scipy` 等本程序用不到的大模块
（避免开发机装了它们时被连带打包），单文件版实测约 **18MB**；
构建结束会自动对产物跑一次 `--selftest` 冒烟测试，失败即报错。

### 自动构建与发布（GitHub Actions）

仓库内置 `.github/workflows/release.yml`：推送 `v*` 标签时（例如 `git tag v1.3.0 && git push origin v1.3.0`）
会在 Windows runner 上自动跑自检、打包 exe 并创建 Release（附件即 `mem_guard.exe`）；
也可在仓库 Actions 页面手动触发，构建产物在 Artifacts 中下载。

打包后的行为：
- 无控制台窗口（`--noconsole`，GUI 子系统）；
- 日志与配置写在 **exe 所在目录**（而非临时解压目录）；
- 托盘「开机自启」直接把 **exe 自身**注册为计划任务（最高权限 / 登录触发 / 无 UAC 弹窗），
  不依赖 Python；仅源码方式运行时才退回 `pythonw + mem_guard.py`；
- 单文件版运行时会解压到临时目录，进程列表里会看到「引导器 + 程序」两个进程，属正常现象
  （日志每次只写一行「MemGuard 启动」，即只有一个托盘实例）。

清理需管理员权限：右键「以管理员身份运行」，或用 `启动 MemGuard（管理员）.bat`（自动提权）。

## 开机自启

- 方式一：运行 `安装 MemGuard 开机自启（管理员）.bat`（注册登录时以最高权限启动的计划任务，无 UAC 弹窗）
- 方式二：在托盘右键菜单勾选「开机自启」
- 卸载：`卸载 MemGuard 开机自启（管理员）.bat`，或在菜单取消勾选

## 配置文件

首次运行会在同目录生成 `mem_guard.json`：

| 键 | 含义 | 默认 |
|---|---|---|
| `phys_threshold` | 物理内存阈值(%) | 85 |
| `commit_threshold` | 提交内存阈值(%) | 90 |
| `interval` | 检测间隔(秒) | 10 |
| `cooldown` | 两次自动清理的冷却(秒) | 300 |
| `auto_clean` | 是否开启自动清理 | true |
| `debounce_sec` | 内存持续超阈值的宽限(秒)，0=立即触发 | 0 |
| `clean_level` | 清理力度：`conservative`(保守，只清缓存) / `aggressive`(激进，额外清空进程工作集) | conservative |
| `user_blacklist` | 额外跳过工作集清空的进程名，如 `["chrome", "code.exe"]`（不区分大小写、可带可不带 `.exe`） | [] |
| `warn_margin` | 距阈值还差多少个百分点时先弹预警，0=关闭预警 | 15 |

> 以上数值都会做合法性钳制（如阈值限 50–99、`interval` 限 2–3600 秒），手误写超范围会自动修正，无需担心配置写坏。

## 优化建议（实用建议）

托盘菜单「优化建议」会根据当前内存状态与你的配置，实时给出分级建议（注意 / 建议 / 提示），
点开即可查看、可「刷新」。下面是一份沉淀自实际踩坑的调优清单：

- **页面文件（虚拟内存）是头号元凶**：Windows 弹「内存不足」时真正耗尽的是提交内存(commit)，
  而提交上限 = 物理内存 + 页面文件。若页面文件过小或被关闭，commit 上限被物理内存顶死，
  很容易在物理还剩几 G 时就弹「内存不足」。建议把页面文件设为**系统托管**，或在其它盘建
  **固定大小**页面文件（仓库附带 `set_pagefile.ps1` + `启动页面文件配置（管理员）.bat`）。
- **清理力度优先选「保守」**：保守档只清系统缓存（standby / 修改页 / 文件缓存），对前台几乎无影响，
  是日常守护的默认选择；激进档额外清空进程工作集，释放更多但前台下次访问需重新读盘、可能卡顿，
  仅在你明确需要更大释放时使用。
- **激进档务必配白名单**：浏览器（Chrome / Edge / Firefox）、IDE（VS Code / Visual Studio / IDEA）、
  Steam、Spotify 等进程在激进档下工作集会被清空而卡顿。把常用程序名加入 `user_blacklist` 即可跳过它们。
  建议引擎检测到这些程序在运行且未加白名单时，会主动提示。
- **阈值关系**：`commit_threshold` 建议高于 `phys_threshold`（默认 90 > 85），因为 commit 含页面文件、
  通常能比物理更高；两者倒挂会削弱监控意义。
- **开启预警 `warn_margin`**：设为 15 之类，可在内存接近阈值时**提前**弹气泡，给你手动干预的机会，
  而不是等到超阈才清理。
- **清理后卡顿怎么办**：多半是激进档清空了某程序工作集，改「保守」或加白名单即可；
  也可调大 `cooldown` 降低清理频率。
- **务必以管理员运行**：清理动作需要特权，非管理员时自动清理不生效（仅记录与提醒）。
  用 `启动 MemGuard（管理员）.bat` 或右键「以管理员身份运行」。
- **疑似内存泄漏**：若某进程 RSS 持续上涨且占用异常高，建议重启该进程；若需长期保留其工作集，
  加入 `user_blacklist`。建议引擎会对高占用进程给出提示。

## 相关脚本

- `set_pagefile.ps1` / `启动页面文件配置（管理员）.bat`：在 D 盘创建固定大小页面文件（缓解提交内存上限过低导致的"内存不足"）
- `install_autostart.ps1`：计划任务注册 / 卸载

## 常见问题

- **提示"需要管理员权限"**：请以管理员身份运行，清理动作需要 `SeProfileSingleProcessPrivilege` 等特权。
- **日志出现 `0xC0000061`（STATUS_PRIVILEGE_NOT_HELD）**：特权未启用或权限不足；当前版本会给出可读提示，并在清理后恢复特权状态。
- **托盘出现两个图标**：旧版本缺少单实例保护；当前版本用命名互斥体防止重复启动。
- **清理后浏览器 / IDE 短暂卡顿**：工作集被清空后需重新读盘所致。改用「清理力度 → 保守」，或把该程序名加入 `user_blacklist`。
- **杀毒软件 / SmartScreen 报毒或拦截**：本工具会调用 `NtSetSystemInformation` 清理内存，行为与 ISLC / Mem Reduct 类似，容易被启发式规则误报。可将其加入杀软白名单；要根治需对 exe 做代码签名（本仓库未签名）。
- **托盘图标偶尔消失**：v1.3.0 起托盘后端异常退出会自动重建；若仍消失，可查 `mem_guard.log` 里有无「托盘异常退出」记录。

## 版本

当前版本 `1.3.11`。

## 源码结构

代码已从单文件 `mem_guard.py` 拆分为 `memguard` 包，便于长期维护：

| 文件 | 职责 |
|---|---|
| `memguard/config.py` | 版本/路径、默认配置、配置校验与钳制、落盘日志（`log`） |
| `memguard/winapi.py` | ctypes 绑定：内存读取（`get_mem`）、特权（启用/禁用/查询）、单实例互斥体、底层清理调用（`_purge_list` / `clear_file_cache`） |
| `memguard/privileges.py` | 清理所需高危特权的启用与「用完即恢复」（`clean_privileges` 上下文管理器） |
| `memguard/actions.py` | 单步清理动作封装：工作集 / 修改页 / standby / 文件缓存，返回原始 NTSTATUS（`purge_working_sets` 等） |
| `memguard/clean.py` | 清理编排（`do_clean`）、进程工作集清空（`empty_process_working_sets`）、Top 进程统计 |
| `memguard/ui.py` | 界面层：托盘图标绘制（`make_icon`）、气泡提示（`message_box`）、Top10 / 趋势 / 优化建议窗口 |
| `memguard/autostart.py` | 开机自启：计划任务注册 / 查询 / 卸载（`autostart_enabled` / `install_autostart` / `remove_autostart`） |
| `memguard/diag.py` | 诊断导出：内存状态 / 进程 / 日志 / 配置打包为 zip（`export_diagnostics`） |
| `memguard/update.py` | 更新检查：查询 GitHub 最新 Release 并比较版本（`fetch_latest_release` / `_parse_version`） |
| `memguard/menu.py` | 托盘菜单：菜单项树构建与全部菜单回调（`build_menu(guard)`） |
| `memguard/tray.py` | 主循环 `Guard`：运行状态、配置热重载、监控循环与托盘图标自愈 |
| `memguard/cli.py` | 入口 `main()`、`--once`、`--selftest`、控制台隐藏等启动流处理 |
| `memguard/advisor.py` | 优化建议引擎：基于内存状态与配置生成分级建议（`analyze` / `format_advice`） |
| `mem_guard.py` | 薄启动器，仅 `from memguard.cli import main`，保持 `python mem_guard.py` 入口不变 |

模块依赖为单向无环：`config` ← `winapi` ← `privileges` / `actions` ← `clean` ← `advisor` ← `ui` ← `autostart` ← `diag` ← `menu` ← `tray` ← `cli`（`update` 仅依赖 `config`）。
