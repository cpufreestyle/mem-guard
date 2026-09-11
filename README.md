# MemGuard — Windows 内存守护托盘工具

实时监控**物理内存**与**提交内存(commit)**，超阈值自动清理并提醒的 Windows 托盘小工具。

## 为什么需要它

Windows 弹「内存不足 / 虚拟内存不足」时，真正耗尽的是**提交内存（commit）**，而不是物理内存。
任务管理器默认不显示 commit，于是经常出现"明明还剩几个 G 却一直弹内存不足"的怪现象。
MemGuard 同时监控这两个指标，并把 commit 放在最显眼的位置。

## 功能

- 托盘图标实时显示物理内存使用率，颜色随压力变化（绿 → 黄 → 红，分档跟随可调阈值）
- 悬停显示物理内存 + 提交内存详情
- 任一指标超阈值时自动清理并弹气泡提醒
- 清理动作（原理同 ISLC / Mem Reduct，调用 `NtSetSystemInformation`）：
  1. 清空进程工作集（EmptyWorkingSet）
  2. 刷写修改页列表（FlushModifiedList）
  3. 清理 standby list（PurgeStandbyList）
  4. 清空系统文件缓存工作集
- 右键菜单：立即清理 / 内存占用 Top10（可刷新窗口）/ 自动清理开关 / 清理阈值（激进·标准·宽松）/ 清理冷却（1/5/10/30 分钟）/ 开机自启开关 / 打开日志 / 退出
- 日志自动轮转（超过 1MB 归档为 `mem_guard.log.1`）
- 单实例运行（命名互斥体）
- 清理所需特权**用完即关**（不常驻 `SeDebugPrivilege` 等高危特权）
- **后台静默运行**：全程无控制台窗口（用 `pythonw.exe` 启动，程序内也会主动隐藏控制台）
- 配置热重载（改 `mem_guard.json` 无需重启）、内存趋势窗口、一键导出诊断
- 超阈值自动清理支持**防抖**（`debounce_sec`），避免内存边缘抖动误触发

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
```

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

## 相关脚本

- `set_pagefile.ps1` / `启动页面文件配置（管理员）.bat`：在 D 盘创建固定大小页面文件（缓解提交内存上限过低导致的"内存不足"）
- `install_autostart.ps1`：计划任务注册 / 卸载

## 常见问题

- **提示"需要管理员权限"**：请以管理员身份运行，清理动作需要 `SeProfileSingleProcessPrivilege` 等特权。
- **日志出现 `0xC0000061`（STATUS_PRIVILEGE_NOT_HELD）**：特权未启用或权限不足；当前版本会给出可读提示，并在清理后恢复特权状态。
- **托盘出现两个图标**：旧版本缺少单实例保护；当前版本用命名互斥体防止重复启动。

## 版本

当前版本 `1.2.1`。
