# 把 MemGuard 放到任务栏 / 桌面上（幂等，可反复执行）
#
#   .\pin_taskbar.ps1                         固定到任务栏
#   .\pin_taskbar.ps1 -Where both             固定到任务栏 + 桌面放一枚快捷方式
#   .\pin_taskbar.ps1 -Where desktop          只建桌面快捷方式，不动任务栏
#   .\pin_taskbar.ps1 -Where both -Mode unpin  任务栏和桌面上的都移除
#   .\pin_taskbar.ps1 -ExePath <p>            指定别的产物（例如 dist\mem_guard\mem_guard.exe）
#
# 不需要管理员权限：只往当前用户的任务栏固定目录 / 桌面写快捷方式。
#
# 为什么不用 shell verb：新版 Windows 11 的 .lnk 右键菜单里只剩「固定到"开始"」，
# 「固定到任务栏」已经被移除了，只能走兜底路径——把快捷方式放进系统的任务栏固定目录，
# 资源管理器会自动把它显示成任务栏按钮：
#   %APPDATA%\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar
#
# 四处细节决定体验：
#   1) 快捷方式带 --show：没在运行时点它要把概览窗带出来，否则用户觉得「点了没反应」；
#      已经在运行时点了它，新进程只会发一个唤窗信号然后静默退出（见 cli.main 的
#      单实例分支），不会变成双实例，也不会再弹一次「已在运行」。
#   2) 快捷方式必须写 System.AppUserModel.ID，且与进程里
#      SetCurrentProcessExplicitAppUserModelID 的值一致（memguard\winapi.py 的
#      APP_USER_MODEL_ID）；否则任务栏上会出现两枚按钮：固定的一枚、运行中的一枚。
#      tests\test_pin.py 会把两边拉出来对账，改一边记得改另一边。
#   3) 快捷方式带「以管理员身份运行」（.lnk 头的 SLDF_RUNAS_USER 位）：清理要动别的
#      进程的工作集，和自带的（管理员）.bat / 计划任务保持同一个权限口径。用户机上
#      点一次可能弹一次 UAC，这是 Windows 的正常行为。
#   4) 图标取 exe 自带的那一枚（打包时 "--icon mem_guard.ico" 写进去的），
#      "$ExePath,0" 即第一个图标资源，任务栏按钮和窗口标题栏才是同一张脸。

param(
    [ValidateSet('pin', 'unpin')]
    [string]$Mode = 'pin',
    [ValidateSet('taskbar', 'desktop', 'both')]
    [string]$Where = 'taskbar',
    [string]$ExePath
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$AppUserModelID = 'MemGuard.MemoryGuard'
$LinkName = 'MemGuard.lnk'
$ScriptArgs = '--show'
$Description = 'MemGuard 内存守护'

$TaskBarDir = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
$TaskBarPath = Join-Path $TaskBarDir $LinkName
$DesktopPath = Join-Path ([Environment]::GetFolderPath('Desktop')) $LinkName

# .lnk 头里 LinkFlags（偏移 20）有一位 SLDF_RUNAS_USER，「以管理员身份运行」就是它。
# 老的兼容性选项卡 / verb 勾选在新版资源管理器上已经不生效，直接翻这个位最稳。
$SLDF_RUNAS_USER = 0x2000
$LINK_HEADER_SIZE = 0x4C

switch ($Where) {
    'taskbar' { $LinkPaths = @($TaskBarPath) }
    'desktop' { $LinkPaths = @($DesktopPath) }
    'both'     { $LinkPaths = @($TaskBarPath, $DesktopPath) }
}

function Set-ShortcutRunAs {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 24) { throw ('快捷方式文件过小：' + $Path) }
    if ([BitConverter]::ToInt32($bytes, 0) -ne $LINK_HEADER_SIZE) {
        throw ('不是标准 .lnk 文件头：' + $Path)
    }
    $flags = [BitConverter]::ToInt32($bytes, 20)
    if ($flags -band $SLDF_RUNAS_USER) { return $false }
    [Array]::Copy([BitConverter]::GetBytes($flags -bor $SLDF_RUNAS_USER), 0, $bytes, 20, 4)
    [System.IO.File]::WriteAllBytes($Path, $bytes)
    return $true
}

if ($Mode -eq 'unpin') {
    foreach ($link in $LinkPaths) {
        if (Test-Path -LiteralPath $link) {
            Remove-Item -LiteralPath $link -Force
            Write-Host ('[+] 已移除：' + $link)
        } else {
            Write-Host ('[i] 本来就没有，跳过：' + $link)
        }
    }
    exit 0
}

# 定位产物：单文件版优先，其次目录版；都没有就明确报错，不静默失败
if (-not $ExePath) {
    foreach ($cand in @((Join-Path $PSScriptRoot 'dist\mem_guard.exe'),
                        (Join-Path $PSScriptRoot 'dist\mem_guard\mem_guard.exe'))) {
        if (Test-Path -LiteralPath $cand) { $ExePath = $cand; break }
    }
}
if (-not $ExePath -or -not (Test-Path -LiteralPath $ExePath)) {
    Write-Error '未找到 mem_guard.exe：先运行 .\build.ps1 打包，或用 -ExePath 指定产物路径'
    exit 1
}
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path
$WorkDir = Split-Path -Parent $ExePath

# WScript.Shell 写基础字段：目标 / 参数 / 工作目录 / 图标 / 说明
$ws = New-Object -ComObject 'WScript.Shell'
try {
    foreach ($link in $LinkPaths) {
        $dir = Split-Path -Parent $link
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $sc = $ws.CreateShortcut($link)
        try {
            $sc.TargetPath = $ExePath
            $sc.Arguments = $ScriptArgs
            $sc.WorkingDirectory = $WorkDir
            $sc.Description = $Description
            $sc.IconLocation = ($ExePath + ',0')
            $sc.Save()
        } finally {
            [System.Runtime.InteropServices.Marshal]::ReleaseComObject($sc) | Out-Null
        }
        Write-Host ('[+] 已写入快捷方式：' + $link)
    }
} finally {
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($ws) | Out-Null
}

# AppUserModelID 不在 WScript.Shell 的能力范围内，用 ShellLink 的属性库补写。
#   PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
#   只读加载会 ACCESSDENIED，所以 SetValue/Commit 走 STGM_READWRITE。
$AumidSource = @'
using System;
using System.Runtime.InteropServices;

namespace MemGuard.Taskbar
{
    public static class PinHelper
    {
        [ComImport]
        [Guid("00021401-0000-0000-C000-000000000046")]
        internal class ShellLink { }

        [ComImport]
        [Guid("0000010B-0000-0000-C000-000000000046")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        internal interface IPersistFile
        {
            void GetClassID(out Guid classId);
            [PreserveSig] int IsDirty();
            void Load([MarshalAs(UnmanagedType.LPWStr)] string fileName, int mode);
            void Save([MarshalAs(UnmanagedType.LPWStr)] string fileName,
                      [MarshalAs(UnmanagedType.Bool)] bool remember);
            void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string fileName);
            void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string fileName);
        }

        [ComImport]
        [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        internal interface IPropertyStore
        {
            [PreserveSig] int GetCount(out int count);
            [PreserveSig] int GetAt(int property, out PropertyKey key);
            [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
            [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
            [PreserveSig] int Commit();
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct PropertyKey
        {
            internal Guid formatId;
            internal int propertyId;
        }

        [StructLayout(LayoutKind.Explicit)]
        internal struct PropVariant
        {
            [FieldOffset(0)] internal short valueType;
            [FieldOffset(8)] internal IntPtr pointer;
        }

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PropVariant value);

        private static PropertyKey AumidKey = new PropertyKey
        {
            formatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
            propertyId = 5
        };

        private const short VtLpwstr = 31;

        public static void SetAppUserModelID(string linkPath, string aumid)
        {
            IPersistFile file = (IPersistFile)(new ShellLink());
            file.Load(linkPath, 1);
            IPropertyStore store = (IPropertyStore)file;
            PropVariant value = new PropVariant();
            value.valueType = VtLpwstr;
            value.pointer = Marshal.StringToCoTaskMemUni(aumid);
            try
            {
                int hr = store.SetValue(ref AumidKey, ref value);
                if (hr < 0) { Marshal.ThrowExceptionForHR(hr); }
                hr = store.Commit();
                if (hr < 0) { Marshal.ThrowExceptionForHR(hr); }
            }
            finally
            {
                PropVariantClear(ref value);
            }
            file.Save(linkPath, true);
            Marshal.ReleaseComObject(file);
        }
    }
}
'@

try {
    Add-Type -TypeDefinition $AumidSource -Language CSharp
    foreach ($link in $LinkPaths) {
        [MemGuard.Taskbar.PinHelper]::SetAppUserModelID($link, $AppUserModelID)
    }
    Write-Host ('[+] 已设置 AppUserModelID：' + $AppUserModelID)
} catch {
    # 固定本身已经成功；只是任务栏按钮可能与运行中的程序不合并
    Write-Host ('[!] AppUserModelID 写入失败（不影响固定，仅任务栏按钮可能不合并）：' + $_.Exception.Message)
}

# RunAs 位最后翻：SetAppUserModelID 会把 .lnk 整存一遍，可能把先翻好的位冲掉
foreach ($link in $LinkPaths) {
    try {
        if (Set-ShortcutRunAs $link) {
            Write-Host '[+] 已勾选「以管理员身份运行」'
        } else {
            Write-Host '[i] 「以管理员身份运行」此前已勾选'
        }
    } catch {
        Write-Host ('[!] 「以管理员身份运行」勾选失败（不影响固定）：' + $_.Exception.Message)
    }
}

Write-Host ''
Write-Host ('[v] 目标  ：' + $ExePath)
Write-Host ('[v] 参数  ：' + $ScriptArgs + '（点一下就把内存概览窗带出来）')
Write-Host ('[v] AUMID ：' + $AppUserModelID + '（与运行进程一致，任务栏只占一个按钮）')
Write-Host '[v] 权限  ：以管理员身份运行（首次点击可能弹一次 UAC，属正常）'
if ($LinkPaths -contains $TaskBarPath) {
    Write-Host '[v] 任务栏没立即出现按钮的话：注销重登一次，或重启资源管理器（explorer.exe）'
}
exit 0

