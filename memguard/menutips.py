# -*- coding: utf-8 -*-
"""
托盘菜单悬停说明：鼠标划过菜单项时，在菜单旁边浮出一句「这项干什么、凭什么动、代价是什么」。

菜单里二十来个开关，文案只有一句「什么时候会动」——「触发线自适应(短效反复且温和手段
用尽时提前)」这种，光看字面猜不出判据、上限和关掉会怎样。本模块补的就是这一层。

为什么不是 tooltip 控件
----------------------
Windows 的普通菜单项（MFT_STRING）没有提示文本 API；TOOLTIPS_CLASS 得用 TTM_ADDTOOL 把
工具挂到一个**窗口**上，而菜单项不是窗口——实测 TTM_ADDTOOL 直接返回 FALSE。所以自己做：

    悬停事件   TrackPopupMenuEx 的 owner 窗口（pystray 的 _menu_hwnd）会收到
               WM_MENUSELECT：wParam 低 16 位是项 id（带子菜单的父项是位置），高 16 位
               是 MF_* 标志，lParam 是 HMENU → GetMenuStringW 原样还原出菜单文案。
    悬浮窗     自己注册窗口类 + DrawTextW 画多行中文，WS_EX_TOPMOST | WS_EX_TOOLWINDOW
               实测能压在正在跟踪的弹出菜单（#32768）之上。它跑在自己的线程与消息泵上：
               托盘线程此刻正卡在 TrackPopupMenuEx 的模态循环里，不能指望它替我们重绘。

子类化必须在 _menu_hwnd 的属主线程里做（跨线程 SetWindowLongPtrW 会被拒），所以 attach()
由 _GuardIcon 在 pystray 建好两个窗口、进主循环之前回调一次——那条调用栈正跑在托盘线程上。

文案按「菜单标签」匹配：静态项走精确表，带计数的动态项（「优化建议（3 条）」）走前缀表，
8 条自适应的说明则从 adapters.ADAPTERS 取——那张表本来就是所有自适应的单一事实来源。
菜单本身（menu.py）因此一行都不用改结构，加一项、改个名，这里补一行文案即可。

依赖方向：config / adapters / winapi ← menutips ← tray。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from . import adapters
from .config import log
from .winapi import (MONITOR_DEFAULTTONEAREST, MONITORINFO, RECT, WNDCLASSEXW, gdi32,
                     kernel32, user32)

# ---------------------------------------------------------------- 文案表
# 只给「看字面猜不出行为」的项写说明：立即清理走哪一档、不受不受冷却约束、
# 关掉之后少掉的是哪一层保护——这些是标签里放不下的。

_STATIC_TIPS = {
    # ---- 动作 ----
    "立即清理": "手动清一次：按当前「清理力度」与「清理区域」执行，不受冷却限制，\n"
              "也不看阈值——内存不高时它照样会清。结果以气泡汇报释放量与 Top3。",
    "内存概览（左键）": "左键点托盘图标是同一个入口。窗口里能看到物理 / 提交内存、"
                "Top10 进程与守护状态；Top5 右键可直接结束进程。",
    "优化建议（": "按当前内存状态与配置现场分析，分级列出可操作项（页面文件、权限、"
              "阈值合理性、进程高占用与白名单、自适应统计、健康态），带一键应用。",
    "一键应用优化建议（": "不弹窗，把上面那套建议里可落地的动作逐条写进配置并热生效。\n"
                  "点下的这一刻按当前配置重新分析，单条失败只跳过该条；没有可应用项时置灰。",
    # ---- 总开关与档位 ----
    # 阈值 / 力度 / 冷却 / 区域 / 触发与阶梯 / 自适应与进阶 / 热键与前台 / 结束进程 /
    # 更新设置 / 快捷键设置都是带子菜单的父项：一展开，左右两侧
    # 就是子菜单的地盘，说明窗无处可放（见 _on_select）。所以这几项的总说明写在
    # 「自动清理」上，各档位的取舍写在它们展开后的子项上——每条文案都得是划得到的。
    # v1.27.0 分组收纳又多了一批父项，父项照样不能挂说明；每组第一条那个不可点的
    # 说明行（menu.py 的 line_*_guide）就是这一组的解释，别把它当成悬停说明的键。
    "自动清理": "自动守护的总开关：关掉后阈值、冷却、定时、预防全都不再动手，"
            "只剩托盘显示与手动清理。快捷键 Ctrl+Alt+P 切的是同一个开关。\n"
            "阈值 = 占用到多少个百分点才动手，越低越勤快；低内存触发 = 可用物理"
            "内存低于该绝对值也清一次，大内存机器上百分比还没到但绝对值已快见底，"
            "靠它兜住；冷却 = 两次自动清理的最小间隔；定时 = 不看占用、到点就清，"
            "日常守护一般用不上。",
    "激进  物理75% / 提交85%": "最早动手的一档：适合经常弹「内存不足」、"
                     "或者页面文件偏小、提交内存顶得快的机器。",
    "标准  物理85% / 提交90%": "默认档，日常守护够用：绝大多数时候轮不到它动手，"
                     "真顶上来时会及时清一次。",
    "宽松  物理92% / 提交95%": "打扰最少的一档：只在快顶不住时才清。"
                     "配了预防式清理时，它会更早按曲线动手。",
    "保守  只清缓存(最温和)": "只清 standby / 修改页 / 文件缓存，对前台几乎无感，"
                    "但释放量有限。日常守护建议就停在这档。",
    "激进  额外清空进程工作集": "释放更多，代价是被清空工作集的进程下次访问要重新读盘、"
                     "可能卡一下。浏览器 / IDE / 游戏建议先加进 user_blacklist。",
    "Standby 列表": "待机页：已经读过、但暂时没人要的页表。清它最安全，"
                "也是保守档释放量的主力。",
    "低优先级 Standby": "低优先级待机页：由程序主动让出的页，回收代价更小。"
                   "关掉它，Standby 那一项仍会照清。",
    "修改页列表": "已改但还没写回的页。清它意味着这些内容要么落盘要么丢弃，"
              "比清 standby 激进一点。",
    "系统文件缓存": "系统读进来的文件缓存。清掉后再次访问这些文件需要重新读盘，"
               "所以大文件反复读的场景（编译、视频剪辑）关掉更顺。",
    "系统工作集（仅激进档）": "只在激进档生效：把各进程的工作集清空，是激进档释放量的主要来源。"
                    "关掉它，激进档就只剩「清得更狠的缓存」，前台卡顿明显减少。",
    "启动时清理": "程序启动后先做一次清理。刚开机时内存压力通常还没起来，"
             "这一条多半只是求个心理安慰，会多一次开机打扰。",
    "清理未达标自动升档": "保守清完仍超阈值、或可用物理仍低于下限时，自动补一次激进清理，"
                 "最多补一次。通知里标「（自动升档）」。关掉后只按你选的档位清。",
    "定向清理内存大户": "保守清完仍超阈值时，先精确清空工作集最大的几个进程，"
                "比直接全量升档打扰小；清完仍不达标才继续升档。"
                "嫌它挑中不该挑的程序，就调大 target_clean_min_mb 或关掉它。",
    "后台进程工作集清理": "排在定向大户之后、全量激进之前：清空没有可见顶层窗口的后台进程。"
                 "覆盖面比定向全、打扰比升档小。窗口枚举不可信时会整级跳过，宁可不clean。",
    # ---- 热键 / 前台 ----
    "全局热键（一键清理/概览/自动清理）": "总开关：Ctrl+Alt+M 立即清理、Ctrl+Alt+O 打开概览、"
                          "Ctrl+Alt+P 切自动清理。键位就在本组下一项「快捷键设置」里"
                          "逐个动作挑："
                          "每项给一组候选组合（至少带一个修饰键，绝不抢 F1 / Ctrl+C "
                          "这类系统快捷键），选「关闭」即单独停用那一条。\n"
                          "某条被别的程序占用时只跳过那一条，托盘与监控不受影响。",
    "全屏应用时暂停自动清理": "检测到全屏应用（游戏 / 全屏播放器）期间暂停自动清理，"
                   "退出后恢复。手动清理、热键、菜单始终可用。"
                   "关掉它，游戏里也可能被来一次工作集清空。",
    "保护前台进程（不清空工作集）": "激进档（含自动升档、粘滞）也不动你正在用的那个进程。"
                      "按「前台窗口是否铺满整块显示器」判定，与进程名无关。"
                      "关掉后前台卡顿的概率明显上升。",
    # ---- 结束进程 ----
    "关闭进程功能": "结束进程总开关。关掉后托盘「结束进程」子菜单、概览窗 Top10 与 "
               "Top5 的右键全部置灰（仍显示，只是点不动）。\n"
               "开着时那个子菜单按内存占用列出前 8 个进程，点一项即结束它，"
               "受六层保护：总开关、确认框、工作集下限、系统进程、自身进程、"
               "结束后确认真的退出了。\n"
               "标「受保护」的行不给点（vmmem 这类虚拟机宿主、csrss 这类系统进程）："
               "它们常占榜首，删掉会让人以为没看见，做成按钮又只会得到一句失败。"
               "vmmem 的内存用「立即清理」就能收回，真要关 WSL 请用 wsl --shutdown。",
    "结束进程前弹确认框": "点选后先弹一次确认再执行 TerminateProcess，防误杀。"
                  "关掉后点一下就直接结束，只剩「工作集下限」这一层保护。",
    # ---- 其它开关 ----
    "持续高压粘滞激进": "上一轮把整条阶梯（保守 → 定向 → 后台 → 激进）走完仍没压住时，"
                "下一轮自动清理直接按激进执行，不再从保守重走一遍。"
                "「保守档不给力自动改激进」关着时它一并失效。",
    "阶段自学习(跳过无效阶段)": "某个阶段连续几次都释放不出东西，就暂时跳过它，"
                      "把力气留给有效的阶段。只影响定向 / 后台两级，不影响升档。",
    "定向清理加深(不达标前再撒宽一轮)": "定向确实清到了东西、但压力还没完全按住时，把网撒宽再清一轮"
                          "（大户数翻倍、下限减半，且只挑上一轮没清过的进程）。"
                          "收益一衰减就收尾，不为「再宽一点」多清一批无关进程。",
    "趋势预防式清理": "还没超阈值，但按历史采样的上升斜率预计在窗口内会触阈时，提前清一次。"
               "嫌它动手太早就关掉；关掉后只在真的越过阈值时才清。",
    "复位自适应闩（": "抹掉「自适应已经自动调优过一次」这段记忆，让那几条重新有机会动手。\n"
                "只清闩：你手动调过的档位、阈值、大户数、预测窗口全部保留。",
    "开机自启": "登录时自动启动托盘（注册计划任务，不是往启动文件夹放快捷方式）。"
            "卸载它请用它旁边那个 .bat。",
    "鼠标悬停显示说明": "划过菜单项时，在旁边浮出这句话：这项干什么、动的是哪一层保护、代价是什么。\n"
                  "只给看不懂的项配说明——划过没弹东西，说明那项的文案自己已经说清楚了。"
                  "嫌它挡地方就关掉，只收起悬浮窗，配置与菜单行为都不受影响。",
    "⚠ 非管理员，无法清理": "清理系统缓存与进程工作集需要「提升单进程权限」，普通权限拿不到，"
                    "所以这一栏是灰的：监控与概览照常，清理一律不动手。\n"
                    "修复：退出后右键 MemGuard 选「以管理员身份运行」；"
                    "开机自启注册的是最高权限计划任务，走它就不必每次手动提权。",
    # ---- 更新 ----
    "自动更新（后台检查）": "后台按 update_check_hours 的间隔静默查询 GitHub Release。"
                    "关掉后只剩手动「检查更新」。",
    "下载后自动安装": "查到新版本后直接下载并替换自身（便携版会先留一份 .bak，"
                "新版本 6 秒内起不来会自动回滚）。关掉后只提醒，由你点「立即更新」。",
    "更新包 sha256 校验": "下载完成后先比对 Release 随包的 checksums.txt，"
                    "对不上就不安装。关掉它等于允许安装来源未校验的包。",
    "导出诊断": "把配置、内存快照、守护状态与最近日志打成一个 zip，"
            "报问题时直接发这个文件即可，不用现场再问「热键到底生没生效」。",
}

# 动态标签（带计数）按前缀匹配
_PREFIX_TIPS = {k: v for k, v in _STATIC_TIPS.items() if k.endswith("（")}
_STATIC_TIPS = {k: v for k, v in _STATIC_TIPS.items() if not k.endswith("（")}

# 8 条自适应的说明来自注册表本身：新增一条自适应只在 ADAPTERS 里写 tip，
# 菜单文案与悬停说明永远不会各说一套。
TIPS = dict(_STATIC_TIPS)
for _a in adapters.ADAPTERS:
    if _a.tip:
        TIPS[_a.label] = _a.tip


def tip_for(label: str):
    """菜单文案 → 悬停说明；没有配说明的项返回 None（此时收起悬浮窗）。"""
    label = (label or "").strip()
    if not label:
        return None
    text = TIPS.get(label)
    if text:
        return text
    best, best_len = None, -1
    for prefix, val in _PREFIX_TIPS.items():
        if label.startswith(prefix) and len(prefix) > best_len:
            best, best_len = val, len(prefix)
    return best


def tip_count() -> int:
    """已配说明的条数（自检与文档用）。"""
    return len(TIPS) + len(_PREFIX_TIPS)


def tip_keys() -> tuple:
    """全部说明键（精确键 + 以「（」结尾的前缀键），供自检与测试对账用。

    两类键分开存是因为前缀键要匹配「优化建议（3 条）」这种带计数的动态文案，
    合并进 TIPS 会让精确查表变慢且难读。对账必须两类一起查，所以这里给个出口，
    别让调用方去摸私有名字。
    """
    return tuple(TIPS) + tuple(_PREFIX_TIPS)


# ---------------------------------------------------------------- Win32 绑定
# 共享的 DLL 句柄与 RECT / MONITORINFO / WNDCLASSEXW 一律从 winapi 取，
# 这里只声明「只有悬停说明会用到」的那几个函数。

WM_MENUSELECT = 0x011F
WM_EXITMENULOOP = 0x0212
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_MG_TIP_SHOW = 0x0400          # 与托盘线程约定的私有消息：显示 / 移动说明
WM_MG_TIP_HIDE = 0x0401

MF_BYCOMMAND = 0x0000
MF_POPUP = 0x0010

GWL_WNDPROC = -4
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_DLGMODALFRAME = 0x00000001
# 说明窗的样式组合：_create 建窗与 _window_size 换算尺寸必须用同一份，改了一处忘了
# 另一处，量出来的高度就会比画出来的少一行。
_TIP_EX_STYLE = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_DLGMODALFRAME
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
DT_WORDBREAK = 0x0010
DT_CALCRECT = 0x0400
DT_EDITCONTROL = 0x2000
DT_NOPREFIX = 0x800
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
ERROR_CLASS_ALREADY_EXISTS = 1410

_TIP_CLASS = "MemGuardMenuTip"
_TIP_MAX_W = 400          # 折行宽度上限（逻辑像素）
_TIP_PAD_X = 9
_TIP_PAD_Y = 7
_TIP_GAP = 6              # 说明窗与菜单之间的间隙
_TIP_BG = 0x00E1FFFF      # COLORREF：经典提示黄
_TIP_FG = 0x00000000
_TIP_MIN_W, _TIP_MIN_H = 90, 26

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                              wintypes.WPARAM, wintypes.LPARAM)


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


for _f, _args, _rest in [
    ("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
    ("CallWindowProcW", [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                         wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
    ("GetMenuStringW", [wintypes.HMENU, ctypes.c_uint, wintypes.LPWSTR,
                        ctypes.c_int, ctypes.c_uint], ctypes.c_int),
    ("GetMenuItemCount", [wintypes.HMENU], ctypes.c_int),
    ("GetMenuItemID", [wintypes.HMENU, ctypes.c_int], wintypes.UINT),
    ("GetMenuItemRect", [wintypes.HWND, wintypes.HMENU, ctypes.c_uint,
                         ctypes.POINTER(RECT)], wintypes.BOOL),
    ("PostMessageW", [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM,
                      wintypes.LPARAM], wintypes.BOOL),
    ("GetMessageW", [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint,
                     ctypes.c_uint], ctypes.c_int),
    ("TranslateMessage", [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
    ("DispatchMessageW", [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
    ("MonitorFromPoint", [ctypes.POINTER(wintypes.POINT), wintypes.DWORD], wintypes.HMONITOR),
    ("GetClientRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
    ("GetDC", [wintypes.HWND], wintypes.HDC),
    ("ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int),
    # DrawTextW 必须显式声明：ctypes 没 argtypes 时把整数按 C int 塞进去，而 GDI 句柄
    # 是 64 位窗口下的指针宽度，值一超过 2^31 就抛 OverflowError——于是量出来的尺寸
    # 永远拿不到，说明窗停在创建时的大小。声明成 HDC 后由 ctypes 自己转指针宽度。
    ("DrawTextW", [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                   ctypes.POINTER(RECT), ctypes.c_uint], ctypes.c_int),
    ("BeginPaint", [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)], wintypes.HDC),
    ("EndPaint", [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)], wintypes.BOOL),
    ("InvalidateRect", [wintypes.HWND, ctypes.POINTER(RECT), wintypes.BOOL], wintypes.BOOL),
    ("UpdateWindow", [wintypes.HWND], wintypes.BOOL),
    ("SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_uint], wintypes.BOOL),
    ("AdjustWindowRectEx", [ctypes.POINTER(RECT), wintypes.DWORD, wintypes.BOOL,
                            wintypes.DWORD], wintypes.BOOL),
    ("FillRect", [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH], ctypes.c_int),
    ("IsWindow", [wintypes.HWND], wintypes.BOOL),
    ("PostQuitMessage", [ctypes.c_int], None),
    ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
]:
    _fn = getattr(user32, _f)
    _fn.argtypes = _args
    _fn.restype = _rest

for _f, _args, _rest in [
    ("CreateFontW", [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT,
                     wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT,
                     wintypes.LPCWSTR], wintypes.HFONT),
    ("SelectObject", [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
    ("SetTextColor", [wintypes.HDC, wintypes.COLORREF], wintypes.COLORREF),
    ("CreateSolidBrush", [wintypes.COLORREF], wintypes.HBRUSH),
]:
    _fn = getattr(gdi32, _f)
    _fn.argtypes = _args
    _fn.restype = _rest

# 窗口类直接复用 winapi 那份 WNDCLASSEXW：它的 lpfnWndProc 声明与上面的 _WNDPROC
# 签名逐字相同，而 ctypes 对同样签名的 WINFUNCTYPE 返回同一个类，赋值能过类型校验。
# 另立一份结构反而要连带改掉 RegisterClassExW 的 argtypes，把探针窗口那份声明带崩。


# ---------------------------------------------------------------- 文案取值


def _menu_text(hmenu, cmd: int) -> str:
    """按命令 id 取菜单项文案；取不到返回空串。"""
    buf = ctypes.create_unicode_buffer(256)
    try:
        n = user32.GetMenuStringW(hmenu, cmd, buf, 255, MF_BYCOMMAND)
    except Exception:
        return ""
    return buf.value if n else ""


def _item_pos(hmenu, cmd: int):
    """命令 id 在该菜单里的位置：GetMenuItemRect 只认位置，不认 id。"""
    try:
        count = int(user32.GetMenuItemCount(hmenu) or 0)
    except Exception:
        return None
    for i in range(count):
        try:
            if int(user32.GetMenuItemID(hmenu, i)) == cmd:
                return i
        except Exception:
            return None
    return None


def _item_rect_at(owner_hwnd, hmenu, pos: int):
    """按位置取屏幕矩形；取不到返回 None。"""
    rc = RECT()
    try:
        if not user32.GetMenuItemRect(owner_hwnd, hmenu, pos, ctypes.byref(rc)):
            return None
    except Exception:
        return None
    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def _item_rect(owner_hwnd, hmenu, cmd: int):
    """被划过那一项的屏幕矩形 (left, top, right, bottom)；取不到返回 None。"""
    pos = _item_pos(hmenu, cmd)
    if pos is None:
        return None
    return _item_rect_at(owner_hwnd, hmenu, pos)


# ---------------------------------------------------------------- 悬浮窗


class _TipWindow:
    """说明窗本体：自己的线程、自己的消息泵。

    单独一条线程不是为了并发，而是因为托盘线程此刻正卡在 TrackPopupMenuEx 的模态
    循环里——把重绘寄托在那条线程上，就得赌它替我们泵 WM_PAINT。
    """

    def __init__(self):
        self.hwnd = 0
        self._thread = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending = None          # (text, rect, above)
        self._text = ""
        self._font = 0
        self._bg_brush = 0

    # -- 生命周期 --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="MemGuardMenuTip",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if not thread or thread is threading.current_thread():
            return
        hwnd = self.hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread.join(2.0)

    # -- 供托盘线程调用 --

    def show(self, text: str, rect) -> None:
        hwnd = self.hwnd
        if not hwnd:
            return
        with self._lock:
            self._pending = (text, rect)
        user32.PostMessageW(hwnd, WM_MG_TIP_SHOW, 0, 0)

    def hide(self) -> None:
        hwnd = self.hwnd
        if not hwnd:
            return
        with self._lock:
            self._pending = None
        user32.PostMessageW(hwnd, WM_MG_TIP_HIDE, 0, 0)

    # -- 线程内部 --

    def _run(self) -> None:
        try:
            self._create()
        except Exception as e:
            log(f"菜单悬停说明窗口创建失败: {e!r}")
            self.hwnd = 0
        finally:
            self._ready.set()
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while True:
            try:
                r = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
            except Exception:
                break
            if r in (0, -1):
                break
            try:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as e:
                log(f"菜单悬停说明消息派发异常: {e!r}")
        self.hwnd = 0

    def _create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        # -14 逻辑像素、GB2312 字符集、PROOF_QUALITY：进程不是 DPI 感知（与全项目
        # 一致，坐标与字号都走 96dpi 虚拟空间），高 DPI 屏上由系统整体缩放。
        self._font = gdi32.CreateFontW(-14, 0, 0, 0, 400, 0, 0, 0, 134, 0, 0,
                                       0x0004, 0x0020, "Microsoft YaHei")
        self._bg_brush = gdi32.CreateSolidBrush(_TIP_BG)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _TIP_PROC_REF
        wc.hInstance = hinst
        wc.hbrBackground = None
        wc.lpszClassName = _TIP_CLASS
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError()
        self.hwnd = int(user32.CreateWindowExW(
            _TIP_EX_STYLE, _TIP_CLASS,
            "MemGuard", WS_POPUP, 0, 0, _TIP_MIN_W, _TIP_MIN_H,
            None, None, hinst, None) or 0)

    def _on_paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        if not hdc:
            return
        try:
            rc = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            user32.FillRect(hdc, ctypes.byref(rc), self._bg_brush)
            old = gdi32.SelectObject(hdc, self._font)
            gdi32.SetTextColor(hdc, _TIP_FG)
            box = RECT(_TIP_PAD_X, _TIP_PAD_Y,
                       rc.right - _TIP_PAD_X, rc.bottom - _TIP_PAD_Y)
            user32.DrawTextW(hdc, self._text, -1, ctypes.byref(box),
                             DT_WORDBREAK | DT_EDITCONTROL | DT_NOPREFIX)
            if old:
                gdi32.SelectObject(hdc, old)
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _measure(self, text: str, hwnd) -> tuple:
        """按 _TIP_MAX_W 折行量一次，返回**客户区**该开多大。

        量字用的是说明窗自己的 DC，不是屏幕 DC：换成屏幕 DC 时字体映射环境不同，
        量出来的行数会比真正画出来的少一截，底部那行就被切掉。
        """
        hdc = user32.GetDC(hwnd)
        box = RECT(0, 0, _TIP_MAX_W - 2 * _TIP_PAD_X, 0)
        try:
            old = gdi32.SelectObject(hdc, self._font)
            user32.DrawTextW(hdc, text, -1, ctypes.byref(box),
                             DT_WORDBREAK | DT_CALCRECT | DT_EDITCONTROL | DT_NOPREFIX)
            if old:
                gdi32.SelectObject(hdc, old)
        finally:
            user32.ReleaseDC(hwnd, hdc)
        return (max(_TIP_MIN_W, int(box.right) + 2 * _TIP_PAD_X),
                max(_TIP_MIN_H, int(box.bottom) + 2 * _TIP_PAD_Y))

    @staticmethod
    def _window_size(client_w: int, client_h: int) -> tuple:
        """客户区尺寸 → 窗口尺寸。

        说明窗带 WS_EX_DLGMODALFRAME，窗口比客户区宽出几像素边框。直接拿客户区尺寸
        去 SetWindowPos，客户区就窄了那几像素，DrawText 折行比量出来的多一行，
        底部那行照样被切——所以必须先换算再摆放。
        """
        rc = RECT(0, 0, client_w, client_h)
        try:
            if user32.AdjustWindowRectEx(ctypes.byref(rc), WS_POPUP, False,
                                         _TIP_EX_STYLE):
                return (max(1, int(rc.right - rc.left)),
                        max(1, int(rc.bottom - rc.top)))
        except Exception:
            pass
        return (client_w, client_h)

    def _place(self, rect, w: int, h: int):
        """贴着菜单放；放不下返回 None（宁可不显示，也不压住用户要点的那张菜单）。

        托盘菜单贴着屏幕右缘，所以默认右侧多半放不下，实际会翻到左侧。
        """
        left, top, right, _bottom = rect
        work = _work_area((right, top))
        x = right + _TIP_GAP
        if x + w > work["right"]:
            x = left - w - _TIP_GAP
        if x < work["left"]:
            x = max(work["left"], work["right"] - w)
        y = top
        if y + h > work["bottom"]:
            y = max(work["top"], work["bottom"] - h)
        x, y = int(max(work["left"], x)), int(max(work["top"], y))
        # 被工作区边缘夹过一轮之后，说明窗可能反过来压住菜单本身——那就别放
        if x < right + 2 and x + w > left - 2:
            return None
        return (x, y)

    def _apply_pending(self, hwnd) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        text, rect = pending
        cw, ch = self._measure(text, hwnd)
        w, h = self._window_size(cw, ch)
        pos = self._place(rect, w, h)
        if pos is None:
            user32.ShowWindow(hwnd, SW_HIDE)
            return
        x, y = pos
        self._text = text
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        user32.InvalidateRect(hwnd, None, True)
        user32.UpdateWindow(hwnd)


def _work_area(point) -> dict:
    """该点所在显示器的工作区（去掉任务栏）；取不到时退回虚拟屏幕。"""
    pt = wintypes.POINT(int(point[0]), int(point[1]))
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    try:
        mon = user32.MonitorFromPoint(ctypes.byref(pt), MONITOR_DEFAULTTONEAREST)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            rc = mi.rcWork
            return {"left": int(rc.left), "top": int(rc.top),
                    "right": int(rc.right), "bottom": int(rc.bottom)}
    except Exception:
        pass
    return {"left": 0, "top": 0,
            "right": int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1920),
            "bottom": int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1080)}


_TIP = _TipWindow()


def _tip_wndproc(hwnd, msg, wparam, lparam):
    """说明窗的窗口过程：只认绘制与两条私有消息，余下全交默认处理。"""
    try:
        if msg == WM_MG_TIP_SHOW:
            _TIP._apply_pending(hwnd)
            return 0
        if msg == WM_MG_TIP_HIDE:
            user32.ShowWindow(hwnd, SW_HIDE)
            return 0
        if msg == WM_PAINT:
            _TIP._on_paint(hwnd)
            return 0
        if msg == WM_ERASEBKGND:
            return 1                       # 背景在 WM_PAINT 里自己填，避免闪白
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
    except Exception as e:
        log(f"菜单悬停说明绘制异常: {e!r}")
    return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))


# 窗口类里的函数指针一旦被回收就成了野指针，回调对象必须常驻。
_TIP_PROC_REF = _WNDPROC(_tip_wndproc)


# ---------------------------------------------------------------- 悬停监听


class MenuTips:
    """把「悬停 → 说明」挂到一个 pystray 图标上；进程内同时只跟一个图标。

    图标可能被重建（托盘自愈），attach 会先把上一个的引用丢掉。旧窗口的子类化随
    窗口一起销毁，WndProc 回调对象则留在 _KEEPALIVE 里——在窗口销毁之前把它回收，
    Windows 就会往野指针上跳。
    """

    def __init__(self, cfg_getter=None):
        self._cfg_getter = cfg_getter
        self._hwnd = 0
        self._prev = None
        self._proc = None
        self._icon = None
        # 这条栈在原生菜单的模态循环里，异常不能越过 ctypes 边界；但全吞掉又会把
        # 「划过没反应」变成无从查起的哑病（DrawTextW 的句柄溢出就是这么藏住的）。
        # 所以第一次异常照常落日志，之后只计数，避免坏掉时把日志刷爆。
        self._error_seen = False

    # -- 开关：关掉时连查表都不做，划过菜单与未实现该特性时表现一致 --

    def enabled(self) -> bool:
        if self._cfg_getter is None:
            return True
        try:
            return bool((self._cfg_getter() or {}).get("menu_tips", True))
        except Exception:
            return True

    def attach(self, icon) -> bool:
        """在托盘线程里调用（_GuardIcon 建好窗口、进主循环之前）。"""
        self.detach()
        _TIP.start()
        hwnd = int(getattr(icon, "_menu_hwnd", 0) or 0)
        if not hwnd:
            log("菜单悬停说明：拿不到 pystray 的菜单宿主窗口，已跳过")
            return False
        proc = _WNDPROC(self._wndproc)
        prev = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC,
                                        ctypes.cast(proc, ctypes.c_void_p))
        if not prev:
            log(f"菜单悬停说明挂载失败: {ctypes.get_last_error()}")
            return False
        self._proc, self._prev, self._hwnd, self._icon = proc, prev, hwnd, icon
        _KEEPALIVE.append(proc)
        return True

    def detach(self) -> None:
        """停掉说明窗并忘掉图标；子类化留给窗口自己去销毁。"""
        _TIP.hide()
        _TIP.stop()
        self._hwnd, self._prev, self._icon = 0, None, None

    # -- 托盘线程里的事件 --

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # 这条栈在原生菜单的模态循环里，异常绝不能越过 ctypes 边界：
        # 越过去不是崩在别人的进程里，就是菜单直接卡住。
        try:
            if msg == WM_MENUSELECT:
                self._on_select(hwnd, wparam, lparam)
            elif msg == WM_EXITMENULOOP:
                _TIP.hide()
        except Exception as e:
            if not self._error_seen:
                self._error_seen = True
                log(f"菜单悬停说明响应异常（后续同类异常不再记录）: {e!r}")
        return int(user32.CallWindowProcW(self._prev, hwnd, msg, wparam, lparam))

    def _on_select(self, hwnd, wparam, lparam) -> None:
        if not self.enabled():
            _TIP.hide()
            return
        ident = wparam & 0xFFFF
        flags = (wparam >> 16) & 0xFFFF
        # 0xFFFF = 菜单收尾（划过空白、退出跟踪都会发它）；MF_POPUP = 带子菜单的父项——
        # 它一展开，左右两侧就都是子菜单的地盘，说明窗无处可放（托盘菜单贴着屏幕右缘，
        # 上方又常被卷出屏幕）。这类项一律收起悬浮窗，解释交给展开后的子项。
        if ident == 0xFFFF or (flags & MF_POPUP):
            _TIP.hide()
            return
        label = _menu_text(lparam, ident)
        text = tip_for(label)
        if not text:
            _TIP.hide()
            return
        rect = _item_rect(hwnd, lparam, ident)
        if rect is None:
            _TIP.hide()
            return
        _TIP.show(text, rect)


_KEEPALIVE = []


def shutdown() -> None:
    """退出前收尾：收起说明窗（供 --selftest 与异常退出路径复用）。"""
    try:
        _TIP.stop()
    except Exception:
        pass
