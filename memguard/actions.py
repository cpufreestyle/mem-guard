# -*- coding: utf-8 -*-
"""
清理动作：把底层 Win32 / Native API 调用封装为单个、可组合的动作。

每个动作返回原始 NTSTATUS（int，0 表示成功）或布尔值，便于上层编排与统计；
本模块不含编排逻辑（编排与结果统计见 clean.py）。

注意：Memory* 系列命令常量定义在 winapi.py，必须显式导入——
遗漏导入只会在真正执行清理（管理员路径）时抛 NameError，非管理员路径不会触发。
"""
from __future__ import annotations

import os

import psutil

from .config import CLEAN_BLACKLIST_STEMS, _blacklist_stems, _norm_proc_name, log, name_in_blacklist
from .winapi import (
    MemoryEmptyWorkingSets,
    MemoryFlushModifiedList,
    MemoryPurgeLowPriorityStandbyList,
    MemoryPurgeStandbyList,
    clear_file_cache,
    kernel32,
    psapi,
    _PROCESS_TERMINATE,
    _purge_list,
)


def purge_working_sets() -> int:
    """清空系统工作集（MemoryEmptyWorkingSets），返回 NTSTATUS。"""
    return _purge_list(MemoryEmptyWorkingSets)


def flush_modified_list() -> int:
    """刷写修改页列表（MemoryFlushModifiedList），返回 NTSTATUS。"""
    return _purge_list(MemoryFlushModifiedList)


def purge_standby_list() -> int:
    """清理 standby list（MemoryPurgeStandbyList），返回 NTSTATUS。"""
    return _purge_list(MemoryPurgeStandbyList)


def purge_low_priority_standby() -> int:
    """只清理 standby list 中的低优先级部分（MemoryPurgeLowPriorityStandbyList）。

    对标 ISLC/WinMemoryCleaner 的「低优先级 standby」清理：影响面比清全部 standby 更小，
    适合作为更温和的第一步。系统不支持该命令时会返回非 0，由上层如实展示。
    """
    return _purge_list(MemoryPurgeLowPriorityStandbyList)


def clear_system_file_cache() -> bool:
    """清空系统文件缓存工作集，返回是否成功。"""
    return clear_file_cache()


def empty_process_working_sets(extra_blacklist=(), exclude_pids=()) -> tuple:
    """逐个进程清空工作集（把不活跃的物理页移到 standby list）。

    extra_blacklist 为用户附加白名单（进程名，可带可不带 .exe，支持 * / ? 通配符），
    命中者跳过，避免清空浏览器/IDE 等工作集造成前台卡顿。返回 (已清理数, 白名单跳过数)。
    exclude_pids 是额外保护的 PID（v1.24.0 前台进程保护）：命中者跳过——激进
    档也不会把用户正在用的那个进程清空，正在被访问的页全在它工作集里。
    """
    return _empty_working_sets(_all_process_pairs(), extra_blacklist,
                               _pid_set(exclude_pids), skip_self=False)


def empty_background_working_sets(exclude_pids=(), extra_blacklist=()) -> tuple:
    """清空「没有可见顶层窗口」的后台进程工作集（定向与升档之间的第三级阶梯）。

    与 empty_process_working_sets 的差别是排除用户正在看的进程：exclude_pids 由
    调用方用 winapi.visible_window_pids() 现算，再叠加自身与黑名单，只对真正后台
    的进程下手——前台零感知，覆盖面又比定向大户全。返回 (已清理数, 跳过数)。
    """
    return _empty_working_sets(_all_process_pairs(), extra_blacklist,
                               _pid_set(exclude_pids), skip_self=True)


def empty_selected_working_sets(pids, extra_blacklist=()) -> tuple:
    """只清空指定 PID 列表对应进程的工作集（定向清理内存大户，波及面最小）。

    与 empty_process_working_sets 的「全量逐进程扫」不同，这里只对传入 PID 精确
    生效：调用方（do_clean 的第二级阶梯）已按工作集挑过大户，这里不再遍历整个
    进程表。每个 PID 仍会重新核对进程名是否命中白名单（采样后进程可能已退出、
    PID 可能被复用），跳过 Idle/System 与单个失败，返回 (已清理数, 跳过数)。
    """
    return _empty_working_sets(_selected_process_pairs(pids), extra_blacklist,
                               (), skip_self=False)


# OpenProcess 所需的最小权限；EmptyWorkingSet / OpenProcess 的类型声明见 winapi.py
_PROCESS_ACCESS = 0x0400 | 0x0100      # PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA

# OpenProcess 失败时的人话原因（错误码 -> 说明）。没列出来的码走兜底文案，
# 至少把码号带给用户，别再让他对着一句「失败」猜。
_TERMINATE_ERROR_REASONS = {
    2: "进程已经不存在（可能刚自己退出了）",
    3: "进程路径无效或已经被回收",
    5: "权限不足：这个进程由 SYSTEM 或别的账户拥有，当前令牌拿不到终止句柄。"
       "以管理员运行也常常不够——清理时临时启用的 SeDebugPrivilege 只在清理那几秒开着，"
       "结束进程不走那条路",
    87: "参数无效（PID 不存在或已被回收）",
    1168: "该进程受系统保护，拒绝打开句柄",
}


def _last_error() -> int | None:
    """取上一次 Win32 调用失败的错误码；取不到返回 None（测试替身常不带 GetLastError）。"""
    try:
        return int(kernel32.GetLastError())
    except Exception:
        return None


def _error_reason(code: int | None, fallback: str) -> str:
    why = _TERMINATE_ERROR_REASONS.get(code)
    if why:
        return why
    return f"{fallback}（错误码 {code}）" if code is not None else fallback


def terminate_process_with_reason(pid: int,
                                  expected_name: str | None = None) -> tuple[bool, str]:
    """terminate_process 的带原因版本：返回 (是否成功, 失败原因)。

    成功时原因为空串。v1.28.0 加这一层，是因为「结束进程 X 失败」这句废话把真正的
    门槛藏住了：vmmemWSL 这类 Session 0 / SYSTEM 拥有的进程，OpenProcess 直接
    ERROR_ACCESS_DENIED，用户在菜单里只会反复点到同一句失败。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        log(f"terminate_process: 非法 PID {pid!r}，已跳过")
        return False, "PID 无效"
    if pid in (0, 4):
        log(f"terminate_process: PID {pid} 是系统保留进程，已跳过")
        return False, "PID 0/4 是系统保留进程"
    if pid == os.getpid():
        log(f"terminate_process: PID {pid} 是本进程，已跳过（自杀保护）")
        return False, "这是 MemGuard 自己"
    if expected_name is not None:
        try:
            actual = psutil.Process(pid).name()
        except Exception:
            log(f"terminate_process: 无法读取 PID {pid} 进程名（可能已退出），已跳过")
            return False, "已经找不到这个进程（可能刚退出）"
        if _norm_proc_name(actual) != _norm_proc_name(expected_name):
            log(f"terminate_process: PID {pid} 名 {actual!r} 与期望 {expected_name!r} 不符（PID 可能已复用），已跳过")
            return False, f"PID {pid} 上现在是另一个进程（{actual}），为防止杀错已停手"
    handle = None
    try:
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            code = _last_error()
            why = _error_reason(code, "系统拒绝打开该进程的终止句柄")
            log(f"terminate_process: OpenProcess(PID={pid}) 失败"
                f"{'' if code is None else f'（错误码 {code}）'}，已跳过")
            return False, why
        ok = bool(kernel32.TerminateProcess(handle, 0))
        if not ok:
            code = _last_error()
            log(f"terminate_process: TerminateProcess(PID={pid}) 返回 False")
            return False, _error_reason(code, "终止请求被系统拒绝")
        return True, ""
    except Exception as e:
        log(f"terminate_process: PID {pid} 异常 {e!r}，已跳过")
        return False, f"调用系统接口时出错（{e!r}）"
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


def terminate_process(pid: int, expected_name: str | None = None) -> bool:
    """强制结束指定 PID 的进程（TerminateProcess），失败-软，绝不抛出。

    保护规则（与清理编排同源，不要另起一套）：
      - PID 0 (系统空闲) 与 PID 4 (系统) 跳过，结束它们会蓝屏/挂机；
      - 跳过本进程（os.getpid()），避免自杀；
      - expected_name 提供时，先用 psutil.Process(pid).name() 现场核对进程名，
        用 _norm_proc_name 比对，不符直接返回 False（防 PID 复用：刚采样到的
        PID 可能已被另一个进程复用，直接 TerminateProcess 会杀错目标）；
      - 任何异常只记日志、返回 False，不影响主流程。

    需要知道「为什么失败」请调 terminate_process_with_reason，本函数是它的布尔外壳。
    """
    return terminate_process_with_reason(pid, expected_name=expected_name)[0]


def _pid_set(pids) -> set:
    """把 PID 序列归一成 int 集合（None / 空序列 → 空集）。"""
    return {int(pid) for pid in pids or ()}


def _empty_working_sets(pairs, extra_blacklist, exclude_pids, skip_self) -> tuple:
    """三条清空路径共用的唯一实现：核对名单 → 开句柄 → 清空 → 关句柄。

    pairs 提供 (pid, name) 来源（全量进程表或按 PID 现场重取），黑名单语义含通配符
    （与用户白名单配置一致，见 config.name_in_blacklist），Idle/System(0/4) 与
    exclude_pids 跳过；skip_self 额外保护本进程。单个进程失败只跳过它自己，
    返回 (已清理数, 跳过数)。
    """
    blocked = CLEAN_BLACKLIST_STEMS | _blacklist_stems(extra_blacklist)
    exclude = set(exclude_pids)
    if skip_self:
        exclude.add(os.getpid())
    count = 0
    skipped = 0
    for pid, name in pairs:
        try:
            if pid in (0, 4) or pid in exclude or name_in_blacklist(name, blocked):
                skipped += 1
                continue
            h = kernel32.OpenProcess(_PROCESS_ACCESS, False, pid)
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        count += 1
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            continue
    return count, skipped


def _all_process_pairs():
    """整个进程表的 (pid, name)；psutil 拿不到的名字是 None。"""
    for p in psutil.process_iter(["pid", "name"]):
        yield p.info["pid"], p.info.get("name")


def _selected_process_pairs(pids):
    """按 PID 现场重取名：上一步采样到现在进程可能已退出、PID 可能被复用。

    取不到名字的 PID 整条丢弃（不计入跳过），与调用方「单个失败不影响其余」一致。
    """
    for pid in pids:
        try:
            pid = int(pid)
            name = None if pid in (0, 4) else psutil.Process(pid).name()
        except Exception:
            continue
        yield pid, name
