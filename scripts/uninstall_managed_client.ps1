<#
CourseLens 受管安装卸载脚本（ARPFIX-1，2026-10）。

用途：卸载 scripts/install_managed_client.ps1 布置的受管安装
（默认位于 %LOCALAPPDATA%\CourseLens），并从系统
「设置 → 应用 → 安装的应用」移除 CourseLens 的卸载入口。

怎么用：
- 平时在「设置 → 应用」里点 CourseLens 的「卸载」即可，就是本脚本。
- 也可以直接运行：powershell -NoProfile -ExecutionPolicy Bypass -File "<本脚本>"
- 想连学习数据一起删除（默认永远不删）：
    powershell -NoProfile -ExecutionPolicy Bypass -File "<本脚本>" -RemoveData

数据红线（与 setup.exe 卸载同一份承诺）：你的学习数据（笔记、进度、登录
状态）默认完整保留。只有显式加 -RemoveData 才会询问，且必须再输入大写 Y
二次确认；其它任何输入——包括无人值守场景下读不到输入——都一律保留数据。

安全口径：
- 只停止「可执行文件位于本安装根目录下」的 python/pythonw 进程（闭集身份，
  与启动器自身的判定一致）；绝不按进程名全盘扫杀无关进程。
- 只删除 launcher/versions/state/trust 四个子目录；删除前先校验
  state\install-layout.json 的受管安装指纹，不像受管安装的目录一律拒绝动。
- 卸载注册项的 GUID 必须与 install_managed_client.ps1 里
  Register-ManagedUninstallEntry 写入的完全一致；两处任一改动都要同步。
#>
param(
    # 安装根目录。缺省 = 本脚本所在目录（安装时它被布置在根目录下，与
    # launcher/versions/state/trust/data 平级）。
    [string]$InstallRoot,
    # 只有显式给出才会询问是否连学习数据一起删除；默认永远保留。
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
# 必须与 scripts/install_managed_client.ps1 的 Register-ManagedUninstallEntry 保持一致（ARPFIX-1）。
$ManagedUninstallKeyName = "{EE7FDDF2-E429-4E21-BEAE-FA0EB749FCB7}"
# 每日定时任务（src/runtime/scheduler_windows.py 注册）；卸载时顺手摘除，
# 否则它会在程序删掉后继续每天空跑一次。
$ManagedDailyTaskName = "FudanCourseLens-DailySync"

$script:HadFailure = $false
$script:DataRoot = $null
$script:DataDeleted = $false

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host $Message
}

function Write-StepResult([string]$Message) {
    Write-Host ("  " + $Message)
}

function Remove-ManagedDirectory {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-StepResult ("{0}：本来就不存在，跳过。" -f $Label)
        return
    }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-StepResult ("{0}：已删除。" -f $Label)
    } catch {
        $script:HadFailure = $true
        Write-StepResult ("{0}：没能完全删除（可能有文件正被其它程序占用）。" -f $Label)
        Write-StepResult ("  别担心，你的学习数据不受影响；稍后可以手动删除这个文件夹：{0}" -f $Path)
    }
}

Write-Host "================================"
Write-Host " CourseLens 卸载"
Write-Host "================================"

# ---- 定位安装根目录 ----
if (-not $InstallRoot) {
    $InstallRoot = Split-Path -Parent $PSCommandPath
}
if (-not (Test-Path -LiteralPath $InstallRoot)) {
    Write-Step ("没有在 {0} 找到 CourseLens 安装目录。" -f $InstallRoot)
    Write-Host "程序本体已经不在了，下面只清理系统里的卸载注册项。"
    $InstallRoot = $null
} else {
    $InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
}

if ($InstallRoot) {
    # ---- 受管安装指纹校验（fail-closed）：不像受管安装的目录一律拒绝删除 ----
    $layoutPath = Join-Path $InstallRoot "state\install-layout.json"
    $layout = $null
    if (Test-Path -LiteralPath $layoutPath -PathType Leaf) {
        try {
            $layout = Get-Content -Raw -Encoding UTF8 $layoutPath | ConvertFrom-Json
        } catch {
            $layout = $null
        }
    }
    if ($null -eq $layout -or $layout.schema -ne "courselens.managed-install.v1") {
        Write-Step ("拒绝卸载：{0} 不像 CourseLens 的受管安装目录（缺少受管安装指纹）。" -f $InstallRoot)
        Write-Host "为了绝不误删别的软件或你的文件，这里一个文件都不会动。"
        Write-Host "如果你确定这就是 CourseLens 的安装目录，先重新运行一次安装脚本，再来卸载。"
        $InstallRoot = $null
        $script:HadFailure = $true
    }
}

if ($InstallRoot) {
    # ---- 第 1 步：停止正在运行的 CourseLens ----
    Write-Step "第 1 步（共 4 步）：停止正在运行的 CourseLens……"
    $closedSetProbe = {
        Get-Process -Name python, pythonw -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($InstallRoot + "\", [StringComparison]::OrdinalIgnoreCase)
        }
    }
    $targets = @(& $closedSetProbe)
    if ($targets.Count -eq 0) {
        Write-StepResult "没有发现正在运行的 CourseLens，直接继续。"
    } else {
        foreach ($process in $targets) {
            Write-StepResult ("发现 CourseLens 进程（PID {0}）：{1}" -f $process.Id, $process.Path)
        }
        foreach ($process in $targets) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction Stop } catch { }
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 250
            $targets = @(& $closedSetProbe)
        } while ($targets.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)
        if ($targets.Count -gt 0) {
            $script:HadFailure = $true
            Write-StepResult "有进程 10 秒内没有退出。先继续后面的步骤；如果删不掉，重启电脑后再运行一次本脚本就好。"
        } else {
            Write-StepResult "已全部停止。"
        }
    }
    # 每日定时任务如果注册过，一并摘除（用户级任务，无需管理员）。
    try {
        & schtasks.exe /Query /TN $ManagedDailyTaskName | Out-Null
        if ($LASTEXITCODE -eq 0) {
            & schtasks.exe /Delete /TN $ManagedDailyTaskName /F | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-StepResult "已移除每日定时任务。"
            } else {
                Write-StepResult "每日定时任务没能移除（不影响其它步骤，也不占什么资源）。"
            }
        } else {
            Write-StepResult "没有注册过每日定时任务，跳过。"
        }
    } catch {
        Write-StepResult "每日定时任务的状态没能确认（不影响其它步骤）。"
    }

    # ---- 第 2 步：删除程序文件（只有这四个子目录，data 永远不在此列） ----
    Write-Step "第 2 步（共 4 步）：删除 CourseLens 程序文件……"
    foreach ($name in @("launcher", "versions", "state", "trust")) {
        Remove-ManagedDirectory -Path (Join-Path $InstallRoot $name) -Label $name
    }

    # ---- 第 3 步：学习数据（默认保留 = 数据红线） ----
    Write-Step "第 3 步（共 4 步）：处理你的学习数据……"
    $script:DataRoot = Join-Path $InstallRoot "data"
    if (-not (Test-Path -LiteralPath $script:DataRoot)) {
        Write-StepResult "没有发现学习数据目录，跳过。"
        $script:DataRoot = $null
    } elseif (-not $RemoveData) {
        Write-StepResult ("你的学习数据已完整保留：{0}" -f $script:DataRoot)
        Write-StepResult "（卸载默认不删学习数据；哪天想连数据一起删，运行本脚本时加上 -RemoveData 参数。）"
    } else {
        $answer = ""
        try {
            $answer = Read-Host "真的连学习数据（笔记、进度、登录状态）一起删除吗？此操作不可恢复。输入大写 Y 确认删除，其它任何输入都会保留数据"
        } catch {
            # 无人值守/无控制台场景读不到输入：按「保留」处理，绝不猜。
            $answer = ""
        }
        if ($answer -ceq "Y") {
            Remove-ManagedDirectory -Path $script:DataRoot -Label "学习数据"
            if (-not (Test-Path -LiteralPath $script:DataRoot)) {
                $script:DataDeleted = $true
                $script:DataRoot = $null
            }
        } else {
            Write-StepResult "没有确认删除，学习数据已保留。"
        }
    }
}

# ---- 第 4 步：清理系统「设置 → 应用」里的卸载注册项 ----
Write-Step "第 4 步（共 4 步）：清理系统里的卸载注册项……"
$uninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" + $ManagedUninstallKeyName
if (Test-Path -LiteralPath $uninstallKeyPath) {
    try {
        Remove-Item -LiteralPath $uninstallKeyPath -Force
        Write-StepResult "已从系统「设置 → 应用」移除 CourseLens 的卸载入口。"
    } catch {
        $script:HadFailure = $true
        Write-StepResult ("卸载注册项没能删除：{0}" -f $_.Exception.Message)
    }
} else {
    Write-StepResult "系统里没有本安装的卸载注册项，跳过。"
}

# 数据已按确认删除、且根目录只剩本脚本时，顺手把残留目录也收走（尽力而为）。
if ($script:DataDeleted -and (Test-Path -LiteralPath $InstallRoot)) {
    $remaining = @(Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $PSCommandPath })
    if ($remaining.Count -eq 0) {
        try {
            Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction Stop
            Remove-Item -LiteralPath $InstallRoot -Force -ErrorAction Stop
            Write-Host ""
            Write-Host "安装目录已清空并移除。"
        } catch {
        }
    }
}

Write-Step "================================"
if ($script:HadFailure) {
    Write-Host "卸载完成，但有个别步骤没能全部处理（见上方说明）。你的学习数据不受影响。"
    exit 1
}
Write-Host "CourseLens 已卸载。"
if ($script:DataDeleted) {
    Write-Host "学习数据已按你的确认一并删除。"
} elseif ($script:DataRoot) {
    Write-Host ("你的学习数据还在：{0}" -f $script:DataRoot)
    Write-Host "以后随时可以重新安装 CourseLens，接着用。"
}
exit 0
