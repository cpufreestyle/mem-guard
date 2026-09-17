# MemGuard 交接文档

> 面向接手维护 / 协作开发的开发者的项目说明。功能与用户向说明见同仓库 `README.md`，本文件聚焦**架构、构建发布、雷区与未结事项**。

## 1. 项目定位（一句话）

Windows 托盘小工具：实时监控**物理内存**与**提交内存(commit)**，超阈值自动清理并提醒，专治「明明还剩几 G 却一直弹『内存不足』」——根因是 commit 耗尽（提交上限 = 物理 + 页面文件），而非物理内存。

- 仓库：`cpufreestyle/mem-guard`（本地 `D:\ai share\repo\mem-guard`，main 分支）
- 最新已发布 tag：`v1.3.8`（CI 自动出 Release，附件 `mem_guard.exe`）
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
| `memguard/config.py` | 版本/路径、默认配置、**配置校验与钳制**、落盘日志 `log` | `__version__`、`load_config`、`normalize_config`、`gb`、`CLEAN_BLACKLIST_STEMS`、`_norm_proc_name`、`_blacklist_stems`、`BASE_DIR`、`LOG_PATH` |
| `memguard/winapi.py` | ctypes 绑定：内存读取、特权启用/禁用/查询、单实例互斥体、底层清理调用 | `get_mem`、`is_admin`、`privilege_state`、`enable_privilege`、`disable_privilege`、`_purge_list`、`clear_file_cache`、`single_instance`、`MemoryEmptyWorkingSets/FlushModifiedList/PurgeStandbyList` |
| `memguard/privileges.py` | **特权管理**：清理所需高特权启用 + 用完即恢复的上下文管理器 | `CLEAN_PRIVILEGES`、`clean_privileges()` |
| `memguard/actions.py` | **清理动作**：单个底层调用的封装（缓存/工作集/修改页/standby） | `purge_working_sets`、`flush_modified_list`、`purge_standby_list`、`clear_system_file_cache`、`empty_process_working_sets` |
| `memguard/clean.py` | **清理编排与统计**：编排各动作、汇总结果；进程 Top 统计 | `do_clean`、`top_processes_list`、`top_processes` |
| `memguard/advisor.py` | 优化建议引擎（基于内存状态+配置生成分级建议） | `analyze`、`format_advice` |
| `memguard/ui.py` | 界面层：图标绘制、气泡、Top10/趋势/建议窗口（不依赖 pystray） | `make_icon`、`message_box`、`show_top_window`、`show_trend_window`、`show_advice_window` |
| `memguard/autostart.py` | 开机自启：计划任务注册/查询/卸载 | `autostart_enabled`、`install_autostart`、`remove_autostart` |
| `memguard/diag.py` | 诊断导出：状态/进程/日志/配置打包 zip | `export_diagnostics` |
| `memguard/update.py` | 更新检查：GitHub 最新 Release 查询与版本比较（纯标准库 urllib） | `fetch_latest_release`、`_parse_version` |
| `memguard/menu.py` | 托盘菜单：菜单项树构建与全部菜单回调（以 `guard` 为参数，不反向 import tray） | `build_menu(guard)` |
| `memguard/tray.py` | 主循环 `Guard`：状态、配置热重载、监控循环、托盘图标自愈 | `Guard` |
| `memguard/cli.py` | 入口 `main()`、`--once`、`--selftest`、控制台隐藏 | `main` |
| `mem_guard.py` | 薄启动器 | — |

**依赖方向单向无环**（新接手改代码时务必保持，避免循环导入）：

```
config ← winapi ← privileges ← actions ← clean ← advisor ← ui ← autostart ← diag ← menu ← tray ← cli
                                                          （update 仅依赖 config）
```

> 注意：`README.md` 的依赖链尚未包含 `privileges.py`/`actions.py`（它们属于工作区未发布的 v1.3.9 候选改动）。提交 v1.3.9 时记得同步 README。

## 4. 关键设计点（改代码前必读）

- **特权用完即关**：清理用的 `SeDebugPrivilege` 等高危特权在 `clean_privileges()` 上下文管理器内启用，退出时把「原本禁用」的恢复禁用，不常驻。
- **清理分两档**：保守（只清系统缓存，默认）/ 激进（额外清进程工作集，前台需重读盘可能卡顿）。
- **进程白名单**：`user_blacklist` 在激进档跳过指定进程工作集；内建 `CLEAN_BLACKLIST_STEMS` 永久跳过系统进程。
- **预警 + 防抖**：`warn_margin` 在接近阈值时只提醒不清理；`debounce_sec` 防止边缘抖动误触发。
- **单实例**：命名互斥体，重复启动直接退出。
- **配置热重载**：`Guard.maybe_reload_config` 比对 `mem_guard.json` 的 mtime，变化即重载；越界值被 `normalize_config` 钳制。
- **托盘自愈**：`Guard.run` 监控线程常驻，图标进程异常退出后自动重建，避免「程序在跑但图标没了」。
- **无控制台窗口**：`--noconsole` 子系统；GUI 下 `sys.stdout is None`，日志走 `log()` 写文件而非 `print`。

## 5. 构建与发布

**本地打包**（产物 `dist\mem_guard.exe`，约 18MB）：

```powershell
pip install pyinstaller
.\build.ps1                 # 默认单文件；结束自动跑 --selftest 冒烟，失败即报错
.\build.ps1 -OneDir         # 目录版（启动快、单进程）
.\build.ps1 -NoTk           # 排除 tkinter（体积小约 10MB，趋势窗口不可用）
.\build.ps1 -Upx            # 启用 UPX 压缩（默认关：易被杀软误报）
```

**自动发布（GitHub Actions）**：`.github/workflows/release.yml` 为 `on: push: tags: ["v*"]`。流程：自检 → PyInstaller 打包 → 冻结版 exe 跑 `--selftest` → 上传 → 创建 Release。

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

- **确认发布状态别用 GitHub API**：匿名 API 限流 60 次/小时，会误判成「404/失败」。改用 `releases.atom` 订阅源或 Releases 网页确认最稳。
- **CI 中文乱码**：GitHub Windows runner 控制台非 UTF-8，Python 打中文会 `UnicodeEncodeError` 致自检失败。CI 已设 `PYTHONUTF8: 1` + `PYTHONIOENCODING: utf-8`，`cli.py` 入口也有 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` 兜底——新增打印中文处别破坏这个。
- **GUI 子系统 exe 退出码**：PowerShell 7（CI runner 默认）下 `& exe; $LASTEXITCODE` 不可靠，必须用 `Start-Process -Wait -PassThru` 取 `.ExitCode`（见 `release.yml` / `build.ps1`）。
- **runner 偶发卡顿**：个别 tag 构建耗时可达 3 分钟（正常约 1 分钟），是排队变慢不是失败，等一会儿再确认。

## 6. 测试现状

- **内置自检**：`python mem_guard.py --selftest` 覆盖 get_mem / make_icon / 配置钳制 / 黑名单匹配 / `_parse_version` / advisor / `build_menu` 等，全 PASS 才说明导入链与基本逻辑 OK。非管理员环境下 `do_clean` 走「需管理员」早返回分支，不会真正清理。
- **pytest 尚未引入（TODO）**：当前没有独立的 `tests/` 与 `requirements-dev.txt`，CI 也不跑 pytest。接手项见 §8。

## 7. 已知问题 / 风险

- **杀软误报（未根治）**：因调用 `NtSetSystemInformation`，行为类似 ISLC/Mem Reduct，易被启发式误报 / SmartScreen 拦截。**代码签名用户已明确搁置**（2026-09-16），目前只能加白名单缓解。若日后要做，需提供证书或接入付费签名服务。
- **非管理员清理无效**：自动清理在非管理员下只记录不执行（这是设计，不是 bug）。
- **历史 bug 已修**：`clean.py` 曾漏导入 `MemoryEmptyWorkingSets/FlushModifiedList/PurgeStandbyList` 三个常量，非管理员路径永不触发所以 `--selftest` 发现不了，**管理员下清理会 `NameError` 失效**。此问题在 v1.3.9 候选改动（`actions.py` 显式导入）中修复，发布 v1.3.9 后即为正式修复。

## 8. 待办 / 接手清单

1. **收尾 v1.3.9（当前工作区未提交改动）**：已新增 `privileges.py`/`actions.py`、`clean.py` 重写、修复上述 `NameError`、自检新增 `build_menu` 用例。接手者需：本地 `--selftest` 全 PASS → 升版本号 → commit → tag `v1.3.9` → push（触发 CI 发布）→ 同步 README 模块表与依赖链。
2. **补 pytest 单元测试（tests/）**：覆盖 `config`/`advisor`/`update`/`ui`/`menu`/`clean`（确定性断言，如注入 `mem` 测 advisor、测 `purge_*` 返回 int 以挡 NameError 类回归）；加 `requirements-dev.txt`，CI 在打包前跑 `pytest`。
3. **安装器（NSIS/Inno Setup）**：生成 `setup.exe` 改善分发；CI 加编译步骤并随 Release 发布（版本号建议从 `config.py` 动态注入 `.iss`/`.nsi`，避免发版时忘改）。
4. **`clean.py` 的 `do_clean` 若进一步拆**：可考虑把结果统计与进程统计再独立，但收益已很低。

## 9. 其他踩坑索引（详见 `.codebuddy/memory` 的 `MEMORY.md`）

- 含中文的 `.ps1` 必须 **UTF-8 with BOM**，否则 PowerShell 5.1 按 GBK 解中文引号破坏语法。
- `git commit -m` 信息里**不要写 `%TEMP%` 这类 `%VAR%`**，会触发「文件名语法不正确」导致整条命令（含 `git add`）失败。
- 后台化 + 输出重定向到工作区文件 = **撑爆磁盘风险**（实测 33GB 直到 C 盘 0 字节）：构建日志写 `$env:TEMP` 并加 `try/catch` 兜底。

---
最后更新：2026-09-16（对应未发布工作区 = v1.3.9 候选）
