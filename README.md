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
  - **保守档（默认）**：1) 清理低优先级 standby list 2) 刷写修改页列表（FlushModifiedList）
    3) 清理 standby list（PurgeStandbyList）4) 清空系统文件缓存工作集 —— 温和，对前台程序几乎无影响
  - **激进档**：在保守档基础上再清空系统/各进程工作集（EmptyWorkingSet）—— 释放更多，但前台程序下次访问需重新读盘
- **清理区域可勾选**（`clean_areas`，对标 WinMemoryCleaner）：standby / 低优先级 standby / 修改页 / 文件缓存 /
  系统工作集逐项开关，托盘菜单「清理区域」即可调整
- **多种触发方式**（对标 Mem Reduct）：超阈值百分比、可用内存低于绝对值（`min_avail_mb`）、
  定时清理（`scheduled_minutes`）、启动时清理（`clean_on_start`）；菜单顶部可看**累计清理次数、释放量与升档 / 定向 / 预防 / 短效次数**
- **进程白名单**（`user_blacklist`）：指定进程在激进档下跳过工作集清空，避免浏览器 / IDE / 游戏被清后卡顿
- **定向清理内存大户**（`target_clean`，默认开）：保守清理后若仍超阈值，不直接全量升档，而是先按工作集挑最大的几个进程精确清空——默认 ≥1GB 的前 3 个，`user_blacklist` 白名单同样生效（逐 PID 重新核对，防 PID 复用）；清完仍不达标才按 `escalate_clean` 补一次激进档。阶梯为「保守 → 定向大户 → 全量激进」，每级最多一次；托盘通知与手动清理通知标注「定向清理大户」并列出入选进程，`--once` 标注「（定向大户）」，`stats.targeted` 累计次数
- **趋势预防式清理**（`predict_clean`，默认开）：把「超阈值才清」的反应式升级为按趋势预判——监控每次采样都把
  `(时刻, 物理%, 提交%)` 攒进 history，用最小二乘拟合上升斜率，预计在 `predict_window_min`（默认 5 分钟）内
  触及阈值，就在真越线之前提前清一次。采样不足（<6 个点或跨度 <60 秒）、斜率低于约 1.2%/分钟、或已越线时
  一律不提前动：不把抖动当趋势，也不抢超阈值清理的活。阶梯仍是「保守 → 定向大户 → 全量激进」（每级最多一次），
  打扰不比一次普通自动清理更多；通知会写明「趋势 +1.5%/分钟，预计 4.0 分钟后触及物理阈值，已提前清理」，
  `stats.preventive` 累计次数
- **清理效果闭环**（`effect_track`，默认开）：只清不看效果等于盲操作——每次自动清理前先量一下距上次自动清理
  多久（上次清理来自手动 / `--once` 时不测，开关关掉也不测），间隔不足 `effect_min_relief_sec`
  （默认 600 秒）即判「效果偏短」：上次清完没 hold 住、内存压力很快复发。短效累计进 `stats.short_relief`
  （非 0 才落键），托盘通知追加「距上次自动清理 5.0 分钟（偏短）」、菜单统计行追加「（短效 N 次）」；
  短效次数过半且 ≥3 次时，与「频繁升档」同源同口径地建议直接改用激进档，并作为档位自调优的第二条判据
  自动切换（一次性、可关）。纯度量、不新增清理动作，打扰只多在通知里一行
- **自适应冷却**（`adaptive_cooldown`，默认开）：静态 `cooldown` 是最大缺口——清理「效果偏短」
  时不会更快复查，连续 hold 住后也不放松。v1.10.0 起让冷却跟着效果走：本次距上次清理不足
  `effect_min_relief_sec`（效果偏短）时，把下次自动清理的最小间隔压到 `max(cooldown * 0.5, floor)`，
  连续 2 次间隔达标再恢复完整 `cooldown`。四处冷却判定（趋势预防 / 超阈值 / 低内存 / 定时）都走
  同一口径；压缩只减不增（`floor` 高于 `cooldown` 也不放大），纯内存态、不写盘，关掉即恒为用户值
- **左键点托盘图标**打开「内存概览」主窗口（对标 WinMemoryCleaner / Mem Reduct）：物理 / 提交两条
  进度条 + 当前占用 Top5（同名多开自动合并为一行）+ 立即清理 / 刷新 / Top10 / 趋势 / 优化建议 快捷按钮
- 右键菜单：内存概览（左键）/ 立即清理 / 内存占用 Top10（可刷新窗口）/ 内存趋势 / 优化建议 / 一键应用优化建议 /
  自动清理开关 /
   清理阈值 / 清理力度 / 清理冷却 / 清理区域 / 定时清理 / 低内存触发 / 启动时清理 /
   清理未达标自动升档 / 定向清理内存大户 / 趋势预防式清理 / 保守频繁升档自动改激进 / 开机自启开关 /
  打开日志 / 导出诊断 / 检查更新 / 退出
- 日志自动轮转（超过 1MB 归档为 `mem_guard.log.1`）
- 单实例运行（命名互斥体）
- 清理所需特权**用完即关**（不常驻 `SeDebugPrivilege` 等高危特权）
- **后台静默运行**：全程无控制台窗口（用 `pythonw.exe` 启动，程序内也会主动隐藏控制台）
- 配置热重载（改 `mem_guard.json` 无需重启）、内存趋势窗口、一键导出诊断
- 超阈值自动清理支持**防抖**（`debounce_sec`），避免内存边缘抖动误触发
- **配置校验**：越界 / 脏配置会被自动钳制回合法范围，不会让程序跑飞
- **托盘常驻**：启动时写 `IsPromoted=1` 尝试把图标留在任务栏可见区；`--pin-tray` 会重启 Explorer 立即生效。**实测限制（2026-10-01）**：当前 Windows 构建上 Explorer 不再为程序化登记的图标建注册表条目，首装须在系统托盘设置里手动拖出一次，之后本机制才有着落
- **托盘自愈**：托盘后端异常退出（如 Explorer 重启）后自动重建图标，避免「程序在跑但图标不见了」
- **自动更新**：托盘后台静默检查 GitHub 最新 Release（默认每 12 小时一次，查询失败或已是最新时
  完全不打扰）；发现新版本每个版本只弹一次气泡，右键「立即更新到最新版」可一键下载安装，
  也可开启「下载后自动安装」全自动升级，详见「自动更新」一节
- **优化建议**：托盘菜单「优化建议」一键分析当前内存状态与配置，给出分级可操作项（页面文件/虚拟内存、权限、阈值合理性、进程高占用与白名单、升档与预防统计自调优（保守档频繁升档、或预防式清理过半时建议直接改激进；关闭升档且超阈值时建议开启）、健康态），其中与自动化开关相关的建议在窗口底部生成「一键应用」按钮（多条建议时另有「全部应用」），点击即增量写入 `mem_guard.json` 并即时生效；无 tkinter 时回退为气泡文本。右键菜单另有「一键应用优化建议（N 项）」入口：点下的那一刻按当前配置重新分析，逐条增量写配置并即时生效，无可应用项时菜单项自动置灰，成功只写日志、不再弹气泡
- **任务栏按钮有图标**：四个 tkinter 窗口都挂了 MemGuard 自己的品牌图标（蓝底内存颗粒），标题栏 / 任务栏按钮 / Alt-Tab 不再显示 Tk 或 Python 的默认图标；配合 `pin_taskbar.ps1` 可固定到任务栏并建桌面快捷方式，见「任务栏图标与桌面快捷方式」

## 界面预览

左键点托盘图标即打开「内存概览」主窗口（无 tkinter 环境自动回退为气泡文本，功能不受影响）：

- **概览窗**：物理 / 提交两条进度条（数值按压力变色）**每秒实时刷新**，占用 Top5 表格每 3s 刷新
  （同名多开如多个 Chrome 合并为「进程 ×N」一行，避免每帧重排刷屏）；底部「立即清理 / 刷新 / Top10 /
  趋势 / 优化建议」直达各功能。
- **Top10 窗**：可刷新的占用排行表格，斑马纹、列宽/对齐按数据类型区分，同样按进程名合并。
- **趋势窗**：物理 / 提交两条曲线，带 0–100% 纵轴刻度、时间刻度与末端当前值标注；首帧不再空白。
- **建议窗**：分级（注意 / 建议 / 提示）着色列出可操作项，可实时刷新。

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

从 Release 安装（普通用户推荐）：下载 `MemGuard-Setup-x.y.z.exe` 双击安装即可——为**当前用户安装**
（无需管理员/UAC），安装时可勾选创建桌面图标；清理内存时程序会提示以管理员身份运行。

命令行辅助模式：

```powershell
python mem_guard.py --once       # 打印一次内存状态并执行一次清理
python mem_guard.py --selftest   # 运行内置自检
python mem_guard.py --help       # 打印用法；写错的参数会直接报错退出，不会静默启动托盘
python mem_guard.py --pin-tray   # 写 IsPromoted 注册表 + 重启 Explorer（首装仍需按下方「托盘常驻」手动拖出一次）
python mem_guard.py --check-update   # 只查一次 GitHub Release 并打印结果，不弹窗、不开浏览器（适合脚本 / 计划任务）
python mem_guard.py --update         # 发现新版本就下载并静默安装，完成后自动重启到新版本
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

生成安装器（可选，需自装 [Inno Setup](https://jrsoftware.org/isinfo.php) 6）：

```powershell
ISCC.exe /DAppVersion=<版本> installer\mem_guard.iss   # 产物 dist\MemGuard-Setup-<版本>.exe
```

### 自动构建与发布（GitHub Actions）

仓库内置 `.github/workflows/release.yml`：推送 `v*` 标签时（例如 `git tag v1.3.0 && git push origin v1.3.0`）
会在 Windows runner 上自动跑单元测试与自检、打包 exe、用 Inno Setup 生成安装器并创建 Release（附件为 `mem_guard.exe` 与 `MemGuard-Setup-x.y.z.exe`）；
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
（绿色版/本地 `dist\mem_guard.exe` 同理：托盘菜单勾选即把 **exe 绝对路径** 注册为登录计划任务。
搬家或重新打包后，exe 路径变了，重新勾选一次即可；桌面图标重新「发送到 → 桌面快捷方式」即可。）

## 任务栏图标与桌面快捷方式

```powershell
.\pin_taskbar.ps1 -Where both                   # 任务栏 + 桌面各放一枚（幂等，可反复执行）
.\pin_taskbar.ps1                              # 只固定到任务栏
.\pin_taskbar.ps1 -Where desktop               # 只建桌面快捷方式，不动任务栏
.\pin_taskbar.ps1 -Where both -Mode unpin      # 两处都撤掉
.\pin_taskbar.ps1 -ExePath dist\mem_guard\mem_guard.exe   # 指定目录版产物
```

- 目标 exe 默认取 `dist\mem_guard.exe`（单文件版），其次 `dist\mem_guard\mem_guard.exe`（目录版），
  与 `build.ps1` 的两种产物对应；找不到会明确报错，不会静默失败。
- **不需要管理员权限**：只往当前用户的任务栏固定目录和桌面写 `.lnk`。
- 快捷方式带 `--show`：程序没在跑时点它，启动后直接把「内存概览」窗带出来（否则用户的感觉就是「点了没反应」）；
  程序已经在跑时点它，新进程只发一个唤窗信号然后静默退出——不会变成双实例，也不会再弹一次「已在运行」。
- 快捷方式写入了 `System.AppUserModel.ID`（`MemGuard.MemoryGuard`），与运行进程里 `SetCurrentProcessExplicitAppUserModelID` 的值一致，
  任务栏上「固定的一枚」和「运行中的一枚」才会并成一个按钮。两边由 `tests\test_pin.py` 自动对账，改一边记得改另一边。
- 快捷方式同时带「以管理员身份运行」（清理要动其它进程的工作集，与 `启动 MemGuard（管理员）.bat` / 开机自启计划任务同一权限口径）。
  **首次点击会弹一次 UAC，这是 Windows 的正常行为**，不是出错。
- 图标取 exe 自带的那一枚（打包时 `--icon mem_guard.ico` 写进去的），任务栏按钮、窗口标题栏、托盘图标是同一张脸；
  四个 tkinter 窗口（概览 / Top10 / 趋势 / 优化建议）也都挂了同一枚窗口图标，不会出现「Python 的 Tk 锤子」。

> Windows 11 的 `.lnk` 右键菜单已移除「固定到任务栏」（只剩「固定到开始」），所以脚本走兜底路径——
> 把快捷方式放进任务栏固定目录 `%APPDATA%\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar`，
> 资源管理器会自动把它显示成任务栏按钮。
> 若任务栏没立即出现按钮，注销重登一次，或重启资源管理器（`explorer.exe`）。

### 托盘常驻（图标固定在任务栏角区）

Windows 会把「出现过一次就不再回来」的托盘图标收进折叠区（任务栏角区那个 ^ 里面）。Explorer
判断要不要常驻一个图标，只看注册表 `HKCU\Control Panel\NotifyIconSettings\<条目>\IsPromoted`，
而这个值**只在 Explorer 启动时读一次**：只写注册表、不重启外壳是无效的——对已存在的条目补发
`NIM_ADD` 也不会让外壳重排（它按内存里已有的排布结果走），必须重启外壳才生效。所以常驻 =
「条目存在」+「IsPromoted=1」+「Explorer 重启外壳」，三样缺一不可。

- 启动时：托盘图标登记完成后顺手把 `IsPromoted=1` 写进注册表，**只写注册表，绝不擅自重启你的外壳**；
  任何失败只记日志，托盘本身照常工作。
- 首装补条目：注册表条目只能由 Explorer 在图标首次登记时创建，程序一次都没起过时条目不存在。
- **实测限制（2026-10-01）**：当前 Windows 构建上，Explorer 不再响应 `NIM_ADD`/`NIM_DELETE`
  自动建条目（探针全程查不到条目），条目的 key 算法也不是 path/uid 哈希。因此**首装时请先在
  系统托盘设置里把 MemGuard 图标手动拖出折叠区一次**，条目生成后 `IsPromoted=1` 才有写入目标；
  拖出这一步无法由程序代劳（UIA / 注册表路线均已实测不可行）。
- 立即生效：`python mem_guard.py --pin-tray`——先写注册表，再重启 Explorer。代价是任务栏会闪一下、
  已打开的资源管理器窗口会关闭，所以命令动手前会先把这两件事说明白。退出码：成功 0，其余 1。
- 条目标识：注册表按**可执行文件绝对路径**区分。dev 实例（`python.exe`）与冻结版
  （`dist\mem_guard.exe`）各拿各的条目，互不干扰；换机器、换目录或重新打包后，重新跑一次该命令即可。
- 判断是否成功：肉眼 / 截图是唯一可信判据。`Shell_NotifyIconGetRect` 会说谎——没有注册表条目的
  活图标照样返回矩形却不渲染（2026-10-01 截图差分证实：slot 零变化），矩形存在不等于图标可见。

## 自动更新

托盘常驻时后台静默检查 GitHub 最新 Release，原则就一条——**能不弹就不弹**：查询失败、网络不通、
已是最新版本，都只在日志里留一行，不弹窗、不开浏览器、不出下载页。

- **检查节奏**：默认每 12 小时一次（`update_check_hours`，可调 1–168 小时）；托盘启动后立刻查一次，
  之后由内存时间戳兜着不会重复发起，把 `interval` 改小也不会让检查变密。
  关掉 `auto_update` 即停掉后台检查（手动菜单项与 CLI 旗标仍可用）。
- **提醒克制**：同一个新版本只弹**一次**气泡（`update_notified_tag` 记账，重启不重复提醒），
  文案带手动入口「右键托盘 → 立即更新到最新版」；此前的版本已提醒过就不翻旧账。
- **手动更新**：右键托盘 → 「立即更新到最新版」，在后台线程里下载安装，**成功不弹任何窗**——
  引导批处理等本进程退出后原地覆盖 / 静默装 Setup，再自动拉起新版本；只有失败才弹一条气泡说原因。
  菜单里「检查更新（v1.5.1）」只查不动手，是最新版也一样安静。
- **全自动**：勾选「下载后自动安装」（`auto_install`）后，检查到新版本会先弹一条「正在下载并安装」
  的气泡，然后直接走安装流程。默认关闭——更新毕竟是替换掉正在运行的程序，默认交给人确认。
- **便携版 vs 安装版自动分流**：安装器部署的目录（有 `unins000.exe`）下载 `MemGuard-Setup-x.y.z.exe`，
  走 /VERYSILENT 原地升级；便携单文件（`dist\mem_guard.exe`）下载便携产物，等进程退出后
  覆盖自身再重启。下载物落在 %TEMP%，引导批处理用完自删，全程无黑窗。
  资产挑选走 `pick_asset()`，永远 **setup 优先、便携兜底**；Release 里找不到可用安装包时
  退回「打开发布页」让人手工下载，不会把用户卡在半截。
- **源码运行模式不自更新**：`python mem_guard.py` 没有可自替换的 exe，点「立即更新」只打开发布页；
  命令行 `--update` 同理失败退出，不影响托盘继续跑。
- **失败不卡死**：下载超时、资产缺失、写脚本失败都在动手之前返回，日志里留可读原因，托盘照常运行，
  下次到点重新检查。

## 配置文件

首次运行会在同目录生成 `mem_guard.json`：

| 键 | 含义 | 默认 |
|---|---|---|
| `phys_threshold` | 物理内存阈值(%) | 85 |
| `commit_threshold` | 提交内存阈值(%) | 90 |
| `interval` | 检测间隔(秒) | 10 |
| `cooldown` | 两次自动清理的冷却(秒)（定时清理同样守该冷却） | 300 |
| `auto_clean` | 是否开启自动清理 | true |
| `debounce_sec` | 内存持续超阈值的宽限(秒)，0=立即触发 | 0 |
| `clean_level` | 清理力度：`conservative`(保守，只清缓存) / `aggressive`(激进，额外清空进程工作集) | conservative |
| `escalate_clean` | 保守清理后若仍超阈值、或可用物理仍低于 `min_avail_mb` 下限，自动补一次激进清理（最多一次、带开关）；关掉则只按 `clean_level` 清理；触发时托盘通知、手动清理通知与 `--once` 均标注「（自动升档）」 | true |
| `target_clean` | 保守清理后仍超阈值时，先按工作集精确清空最大的几个进程（比直接全量升档打扰小），清完仍不达标才走 `escalate_clean`；触发时托盘通知、手动清理通知标注「定向清理大户」，`--once` 标注「（定向大户）」 | true |
| `target_clean_min_mb` | 工作集达到该值(MB)的进程才算「内存大户」，限 128–32768 | 1024 |
| `target_clean_top` | 单次最多精确清空前 N 个工作集最大的进程，限 1–10 | 3 |
| `auto_level_adapt` | 档位自调优：保守档累计清理中升档占比过半且 ≥3 次时，自动把 `clean_level` 改为 `aggressive`（一次性，切换后发一次托盘通知，菜单「保守频繁升档自动改激进」可提前关） | true |
| `predict_clean` | 趋势预防式清理：未超阈值、但按历史采样拟合的上升斜率预计在预测窗口内触阈时，提前清一次（阶梯仍是「保守 → 定向大户 → 全量激进」）；触发时托盘通知标注「趋势 +1.5%/分钟，预计 4.0 分钟后触及物理阈值，已提前清理」，`stats.preventive` 累计次数，菜单「趋势预防式清理」可关 | true |
| `predict_window_min` | 预防判定的预测窗口(分钟)：预计触阈时间落在该窗口内才提前清理，限 1–60 | 5 |
| `effect_track` | 清理效果闭环：自动清理前度量与上次自动清理的间隔，不足 `effect_min_relief_sec` 判为「效果偏短」并累计 `stats.short_relief`（上次清理来自手动 / `--once`、或开关关闭时不度量）；通知追加「距上次自动清理 N 分钟（偏短）」、统计行追加「（短效 N 次）」 | true |
| `effect_min_relief_sec` | 「效果偏短」的间隔下限(秒)：距上次自动清理不足该值即算上次没 hold 住，限 60–86400 | 600 |
| `adaptive_cooldown` | 自适应冷却：清理「效果偏短」时把下次自动清理的最小间隔压到 `max(cooldown * 0.5, adaptive_cooldown_floor)`，连续 2 次间隔达标再恢复完整 `cooldown`（四处冷却判定共用；压缩只减不增、纯内存态） | true |
| `adaptive_cooldown_floor` | 压缩后的最小间隔下限(秒)：只在压到更低时兜底，高于 `cooldown` 也不放大，限 5–3600 | 30 |
| `level_adapt_done` | 档位自调优的闩：已自动切过激进的标记，挡住重复覆盖；v1.10.0 起用户手动把档位切回保守会自动清闩重新武装 | false |
| `user_blacklist` | 额外跳过工作集清空的进程名，如 `["chrome", "code.exe"]`（不区分大小写、可带可不带 `.exe`） | [] |
| `warn_margin` | 距阈值还差多少个百分点时先弹预警，0=关闭预警 | 15 |
| `advice_refresh_sec` | 后台刷新「优化建议条数」的最小间隔(秒)，限 15–600；越低越跟手但越费 CPU | 60 |
| `min_avail_mb` | 可用物理内存低于该值(MB)也触发清理，0=关闭 | 0 |
| `scheduled_minutes` | 每隔 N 分钟主动清理一次（不看内存占用），0=关闭 | 0 |
| `clean_on_start` | 启动后先清理一次 | false |
| `clean_areas` | 清理区域开关：`standby` / `low_priority_standby` / `modified` / `file_cache` / `working_sets` | 全部 true |
| `auto_update` | 后台静默检查更新的总开关，关掉后托盘不再自动查询 | true |
| `update_check_hours` | 后台检查更新的间隔(小时)，限 1–168 | 12 |
| `auto_install` | 检查到新版本是否直接下载并自动安装（否则只提醒，由你点「立即更新」） | false |
| `last_update_check` | 上次检查更新的时间戳(秒)，程序自动维护 | 0 |
| `update_notified_tag` | 已气泡提醒过的最新版本号，用于每版本只提醒一次 | "" |
| `stats` | 累计统计（清理次数 / 释放量 / 升档次数 / 定向次数 / 预防次数 / 短效次数），程序自动维护，菜单顶部可见 | {"count": 0, "freed": 0} |

> 以上数值都会做合法性钳制（如阈值限 50–99、`interval` 限 2–3600 秒），手误写超范围会自动修正，无需担心配置写坏。

> 改完保存即生效（热重载，不用重启托盘）；托盘菜单里的改动会在当前配置之上增量合并，不会覆盖你刚手改的字段，多个改动入口之间也不会互相冲掉。

## 优化建议（实用建议）

托盘菜单「优化建议」会根据当前内存状态与你的配置，实时给出分级建议（注意 / 建议 / 提示），
点开即可查看、可「刷新」；与自动化开关相关的建议带「一键应用」按钮，点一下即写入配置并热生效（多条建议可「全部应用」，单条失败只跳过该条）。下面是一份沉淀自实际踩坑的调优清单：

不想开窗口？右键菜单的「一键应用优化建议（N 项）」把同样一套动作搬到了托盘上：计数 N 由后台按
`advice_refresh_sec` 低频刷新，点下的那一刻按当前配置重新分析（避免拿过期结论写盘），逐条增量合并写入配置并即
时生效——单条失败只跳过该条，绝不一损俱损；没有可应用项时菜单项直接置灰，成功后只写一条日志、不再弹气泡，
把「最小打扰」贯彻到底。

- **清理「效果偏短」也是改激进档的信号**：v1.9.0 起每次自动清理前都会量一下距上次自动清理多久，间隔不足
  `effect_min_relief_sec`（默认 600 秒）就记一次 `stats.short_relief`——上次清完没 hold 住、内存很快又被顶回去。
  短效次数过半且 ≥3 次时，建议引擎会出一条「保守清理效果不佳，建议直接改用激进」（可一键应用），
  档位自调优也会据此自动切一次激进档。托盘通知里那行「距上次自动清理 5.0 分钟（偏短）」说的就是它；
  只想安静清理可在 `mem_guard.json` 里把 `effect_track` 关掉。

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
  也可调大 `cooldown` 降低清理频率。若只是被「定向清理大户」清空的程序卡顿，可调大
  `target_clean_min_mb` 提高入选门槛，或关掉菜单里的「定向清理内存大户」。老是还没超阈值就被清，可关掉菜单里的「趋势预防式清理」，或把 `predict_window_min` 调小。
- **务必以管理员运行**：清理动作需要特权，非管理员时自动清理不生效（仅记录与提醒）。
  用 `启动 MemGuard（管理员）.bat` 或右键「以管理员身份运行」。
- **疑似内存泄漏**：若某进程 RSS 持续上涨且占用异常高，建议重启该进程；若需长期保留其工作集，
  加入 `user_blacklist`。建议引擎会对高占用进程给出提示。

## 相关脚本

- `set_pagefile.ps1` / `启动页面文件配置（管理员）.bat`：在 D 盘创建固定大小页面文件（缓解提交内存上限过低导致的"内存不足"）
- `install_autostart.ps1`：计划任务注册 / 卸载
- `pin_taskbar.ps1`：固定到任务栏 / 建桌面快捷方式（幂等，不需要管理员权限，可用 `-Mode unpin` 撤掉）

## 常见问题

- **提示"需要管理员权限"**：请以管理员身份运行，清理动作需要 `SeProfileSingleProcessPrivilege` 等特权。
- **日志出现 `0xC0000061`（STATUS_PRIVILEGE_NOT_HELD）**：特权未启用或权限不足；当前版本会给出可读提示，并在清理后恢复特权状态。
- **托盘出现两个图标**：旧版本缺少单实例保护；当前版本用命名互斥体防止重复启动。
- **清理后浏览器 / IDE 短暂卡顿**：工作集被清空后需重新读盘所致。改用「清理力度 → 保守」，或把该程序名加入 `user_blacklist`。
- **杀毒软件 / SmartScreen 报毒或拦截**：本工具会调用 `NtSetSystemInformation` 清理内存，行为与 ISLC / Mem Reduct 类似，容易被启发式规则误报。可将其加入杀软白名单；要根治需对 exe 做代码签名（本仓库未签名）。
- **托盘图标偶尔消失**：v1.3.0 起托盘后端异常退出会自动重建；若仍消失，可查 `mem_guard.log` 里有无「托盘异常退出」记录。
- **托盘图标被收进折叠区（要戳 ^ 才看得见）**：首装请在系统托盘设置里手动拖出一次；拖出后 `python mem_guard.py --pin-tray` 会写 `IsPromoted=1` 并重启 Explorer 让它保持，原理见「托盘常驻」。
- **只写了注册表、图标却没动静**：启动时只写 `IsPromoted=1`、不重启外壳，Explorer 仍按内存里的旧排布走。
  跑一次 `python mem_guard.py --pin-tray` 重启外壳即生效；日志里「托盘常驻已声明」说的就是这种状态。
- **跑了 `--pin-tray` 仍在折叠区**：先确认命令作用的是**当前在跑的那份程序**——条目按可执行文件绝对路径各存一份
  （dev 是 `python.exe`，绿色版是 `dist\mem_guard.exe`）；若条目压根没被 Explorer 建出来（首装未手动拖出），请在托盘设置里手动拖出一次再跑该命令。

## 版本
- **通知里出现「距上次自动清理 5.0 分钟（偏短）」**：这是清理效果闭环在度量「上次清理有没有 hold 住」——间隔不足
  `effect_min_relief_sec`（默认 600 秒）即算效果偏短，只累计 `stats.short_relief` 供统计行与档位自调优判断，
  不影响清理动作本身；想关掉可在 `mem_guard.json` 里把 `effect_track` 改为 `false`（本项没有菜单开关）。
- **清理突然变勤 / 又变回原节奏**：这是自适应冷却在跟着效果走——距上次清理不足 `effect_min_relief_sec`（效果偏短）
  时把最小间隔压到 `max(cooldown * 0.5, 30)`，连续 2 次间隔达标再恢复完整 `cooldown`。只想固定节奏可在 `mem_guard.json` 里
  把 `adaptive_cooldown` 改为 `false`。

当前版本 `1.10.0`。本版新增自适应冷却（`adaptive_cooldown`，默认开）：静态 `cooldown` 是最大缺口——清理效果偏短时不会更快复查，连续 hold 住后也不放松。现在让冷却跟着效果走：本次自动清理距上次不足 `effect_min_relief_sec`（效果偏短）时，把下次自动清理的最小间隔压到 `max(cooldown * 0.5, adaptive_cooldown_floor)`，连续 `_COOLDOWN_OK_STREAK`（2）次间隔达标再恢复完整 `cooldown`。四处冷却判定（趋势预防 / 超阈值 / 低内存 / 定时）共用 `Guard._cooldown_gap()` 一个口径；压缩只减不增——clamp(0, cooldown)，floor 高于 cooldown 也不放大；纯内存态、不落盘，关掉即恒为用户值。上一版 `1.9.0`：清理效果闭环（`effect_track`，默认开）：把「清完就完」补上效果度量——每次自动清理前先算距上次自动清理多久（上次清理来自手动 / `--once` 时不测，开关关掉也不测），间隔不足 `effect_min_relief_sec`（默认 600 秒）即判「效果偏短」：上次清理没 hold 住、内存压力很快复发。短效累计进 `stats.short_relief`（非 0 才落键，不污染干净配置），托盘通知追加「距上次自动清理 5.0 分钟（偏短）」、菜单统计行追加「（短效 N 次）」；短效次数过半且 ≥3 次时（与频繁升档、频繁预防同源同口径）advisor 出一条「保守清理效果不佳，建议直接改用激进」并可一键应用，同时作为档位自调优的第二条判据自动切一次激进档（一次性、落闩、只发一条通知、可提前关）。纯度量、不新增清理动作，打扰只多在通知里一行。
上一版 `1.8.0`：趋势预防式清理（`predict_clean`，默认开）：趋势预防式清理（`predict_clean`，默认开）：监控循环每次采样都把 `(时刻, 物理%, 提交%)` 攒进 history，用最小二乘拟合上升斜率，预计在 `predict_window_min`（默认 5 分钟）内触及阈值，就在真越线之前提前清一次——从「超阈值才清」的反应式升级为按趋势预判的预防式。门：采样够多（默认 ≥6 个点且跨度 ≥60 秒）、斜率 ≥约 1.2%/分钟、ETA 落在窗口内、且当前未越线；任一不满足都不提前打扰，也不抢超阈值清理的活。阶梯仍走「保守 → 定向大户 → 全量激进」（每级最多一次）；清理通知标注「趋势 +1.5%/分钟，预计 4.0 分钟后触及物理阈值，已提前清理」，同一次 tick 不再重复弹接近阈值预警；`stats.preventive` 累计次数并在菜单统计行显示「（预防 N 次）」；预防次数过半且 ≥3 次时 advisor 建议直接改用激进档（与频繁升档同源同口径）；菜单「趋势预防式清理」可关。上一版 `1.7.0`：定向清理内存大户（`target_clean`，默认开）：保守清理后若仍超阈值，先按工作集挑最大的几个进程精确清空（默认 ≥1GB 的前 3 个，`user_blacklist` 白名单生效、逐 PID 重新核对防 PID 复用），清完仍不达标才按 `escalate_clean` 补一次激进档——阶梯「保守 → 定向大户 → 全量激进」，每级最多一次、最小打扰；托盘通知与手动清理通知标注「定向清理大户」并列出入选进程与工作集，`--once` 档位行加「（定向大户）」，`stats.targeted` 累计次数并在菜单统计行显示「（定向 N 次）」，菜单「定向清理内存大户」可关。上一版 `1.6.0`：档位自调优（v1.5.1 起升档次数计入 `stats.escalated`；保守档累计清理中升档占比过半且 ≥3 次，说明「先温和再彻底」每次都在白跑一遍，遂自动把 `clean_level` 切成 `aggressive` 并落闩 `level_adapt_done`——一次性、切换只发一条托盘通知、用户手动切回保守也不再自动评估、菜单「保守频繁升档自动改激进」可提前关；判据与 advisor「保守档频繁升档，建议直接改用激进」同源）。同版新增「优化建议一键应用」：advisor 为可操作建议附带 `action`（自动清理已关→开启、预警未开→设 15%、冷却过短→调为 300s、保守频繁升档→改激进、升档关闭且超阈值→开启），建议窗口据此渲染「一键应用」按钮，点击即增量合并写入配置并热生效（写入沿用同一把配置锁，单条失败只跳过该条，无 tkinter 时退化为普通文本）。同版再把同一套动作搬上右键菜单：「一键应用优化建议（N 项）」的计数由后台按 `advice_refresh_sec` 低频刷新，点下的那一刻按当前配置重新分析后逐条增量写配置、即时生效（单条失败只跳过该条），无可应用项时菜单项置灰，成功只写日志不弹气泡。上一版 `1.5.1` 为自动升档清理（保守清理后仍超阈值、或可用物理仍低于 `min_avail_mb` 下限，即补一次激进清理；最多一次、菜单可关、升档时通知与 `--once` 标注「（自动升档）」、最小打扰）。

## 源码结构

代码已从单文件 `mem_guard.py` 拆分为 `memguard` 包，便于长期维护：

| 文件 | 职责 |
|---|---|
| `memguard/config.py` | 版本/路径、默认配置、配置校验与钳制、落盘日志（`log`） |
| `memguard/winapi.py` | ctypes 绑定：内存读取（`get_mem`）、进程快路径枚举（`process_working_sets`，Toolhelp32 + `GetProcessMemoryInfo`）、特权（启用/禁用/查询）、单实例互斥体、底层清理调用（`_purge_list` / `clear_file_cache`）、托盘常驻（`IsPromoted` 读写 / `Shell_NotifyIconGetRect` / 重启 Explorer） |
| `memguard/privileges.py` | 清理所需高危特权的启用与「用完即恢复」（`clean_privileges` 上下文管理器） |
| `memguard/actions.py` | 单步清理动作封装：工作集 / 修改页 / standby / 文件缓存，返回原始 NTSTATUS（`purge_working_sets` / `empty_selected_working_sets` 等） |
| `memguard/clean.py` | 清理编排（`do_clean`）、进程工作集清空（`empty_process_working_sets` / `empty_selected_working_sets`）、定向大户挑选与清理（`_target_clean_candidates` / `_run_target_clean`）、趋势斜率拟合与预防判定（`_slope_per_sec` / `predictive_due`）、Top 进程统计 |
| `memguard/ui.py` | 界面层：抗锯齿托盘图标绘制（`make_icon`，4× 超采样）、气泡提示（`message_box`）、内存概览（`show_overview_window`）/ Top10 / 趋势 / 优化建议窗口（`apply_advice_actions` 支持「一键应用」/「全部应用」） |
| `memguard/autostart.py` | 开机自启：计划任务注册 / 查询 / 卸载（`autostart_enabled` / `install_autostart` / `remove_autostart`） |
| `memguard/diag.py` | 诊断导出：内存状态 / 进程 / 日志 / 配置打包为 zip（`export_diagnostics`） |
| `memguard/update.py` | 自动更新：查询 GitHub 最新 Release、版本比较、资产挑选、下载与静默安装 / 自替换（`check_and_notify` / `install_latest` / `pick_asset`） |
| `memguard/menu.py` | 托盘菜单：菜单项树构建与全部菜单回调（`build_menu(guard)`，含「一键应用优化建议」`on_apply_advice`——现场重新分析后逐条增量写配置；「定向清理内存大户」`on_toggle_target_clean`；「趋势预防式清理」`on_toggle_predict_clean`） |
| `memguard/tray.py` | 主循环 `Guard`：运行状态、配置热重载、监控循环、趋势预防式清理等触发源的自动清理、托盘图标自愈与常驻声明 |
| `memguard/cli.py` | 入口 `main()`、`--once`、`--selftest`、`--check-update`、`--update`、`--pin-tray`、控制台隐藏等启动流处理 |
| `memguard/advisor.py` | 优化建议引擎：基于内存状态与配置生成分级建议（`analyze` / `format_advice` / `advice_actions`——把可操作建议映射成可一键应用的配置改动；`frequent_prevention`——预防式清理过半时建议直接改激进） |
| `mem_guard.py` | 薄启动器，仅 `from memguard.cli import main`，保持 `python mem_guard.py` 入口不变 |

模块依赖为单向无环：`config` ← `winapi` ← `privileges` / `actions` ← `clean` ← `advisor` ← `ui` ← `autostart` ← `diag` ← `menu` ← `tray` ← `cli`（`update` 仅依赖 `config`）。
