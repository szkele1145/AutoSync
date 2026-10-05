# 构建 AutoSync 的 MCDR 插件包（server/mcdr/AutoSync-1.0.0.mcdr）
#
# 做的事：
#   1. 把 server/python/autosync 同步到 server/mcdr/autosync/core（MCDR 版与独立版共用同一套核心代码）
#   2. 清掉 __pycache__ / *.pyc
#   3. 校验入口可用（tools/verify-mcdr-entry.py：装过 mcdreforged 就用真的，否则用 tests/mcdr_stub）
#   4. 检查包结构（tools/check-mcdr-package.py）
#   5. 打成 zip 再改名成 .mcdr（Compress-Archive 不接受 .mcdr 后缀，直接压会被拒）
#
# 包内结构（MCDR 要求入口必须在与插件 id 同名的包内，所以核心代码放在 autosync/core/）：
#   mcdreforged.plugin.json      entrypoint = autosync.entry
#   autosync/__init__.py         版本号 / 包说明
#   autosync/entry.py            MCDR 入口（仓库里的唯一副本）
#   autosync/core/*.py           独立版核心代码的同步副本
#   config.example.json          默认配置参考
#
# 用法：
#   pwsh -File tools/build-mcdr.ps1                # 同步 + 校验 + 打包
#   pwsh -File tools/build-mcdr.ps1 -SkipVerify    # 跳过校验（不推荐）
#   pwsh -File tools/build-mcdr.ps1 -Python "C:\path\to\python.exe"   # 指定带 mcdreforged 的解释器
#
# 版本号：只改这里一处（或改 server/python/autosync/__init__.py 的 __version__）

param(
    [string]$Version = "1.0.0",
    [string]$Python = "",
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonSrc = Join-Path $RepoRoot "server\python\autosync"
$McdrDir = Join-Path $RepoRoot "server\mcdr"
$McdrPkg = Join-Path $McdrDir "autosync"
$McdrCore = Join-Path $McdrPkg "core"
$EntryFile = Join-Path $McdrPkg "entry.py"
$PluginJson = Join-Path $McdrDir "mcdreforged.plugin.json"
$ConfigExample = Join-Path $McdrDir "config.example.json"
$OutFile = Join-Path $McdrDir "AutoSync-$Version.mcdr"
$Staging = Join-Path ([System.IO.Path]::GetTempPath()) ("autosync-mcdr-build-" + [System.Guid]::NewGuid().ToString("N"))

Write-Host "== AutoSync MCDR 打包 v$Version"
Write-Host "   仓库根目录 : $RepoRoot"
Write-Host "   打包输出   : $OutFile"

# ---------------------------------------------------------------- 1. 同步核心代码
if (-not (Test-Path $PythonSrc)) { throw "找不到独立版源码目录：$PythonSrc" }
if (-not (Test-Path $EntryFile)) { throw "找不到 MCDR 入口：$EntryFile" }
if (-not (Test-Path $PluginJson)) { throw "找不到插件元数据：$PluginJson" }

if (Test-Path $McdrCore) { Remove-Item $McdrCore -Recurse -Force }
Copy-Item $PythonSrc $McdrCore -Recurse -Force
Write-Host "   已同步核心代码：server/python/autosync -> server/mcdr/autosync/core"

# ---------------------------------------------------------------- 2. 清理缓存
Get-ChildItem $McdrDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
Get-ChildItem $McdrDir -Recurse -File -Include "*.pyc", "*.pyo" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Force }

# 确认两边内容一致（除 __pycache__ 外应完全相同）
$srcFiles = Get-ChildItem $PythonSrc -Recurse -File |
    Where-Object { $_.Extension -ne ".pyc" } |
    ForEach-Object { $_.FullName.Substring($PythonSrc.Length + 1) } | Sort-Object
$dstFiles = Get-ChildItem $McdrCore -Recurse -File |
    Where-Object { $_.Extension -ne ".pyc" } |
    ForEach-Object { $_.FullName.Substring($McdrCore.Length + 1) } | Sort-Object
$diff = Compare-Object $srcFiles $dstFiles
if ($diff) {
    Write-Host "!! 同步后文件列表不一致：" -ForegroundColor Red
    $diff | Format-Table -AutoSize | Out-String | Write-Host
    throw "autosync/core/ 同步失败"
}
$changed = 0
foreach ($rel in $srcFiles) {
    $a = Get-FileHash (Join-Path $PythonSrc $rel) -Algorithm SHA256
    $b = Get-FileHash (Join-Path $McdrCore $rel) -Algorithm SHA256
    if ($a.Hash -ne $b.Hash) { $changed++ }
}
if ($changed -ne 0) { throw "autosync/core/ 有 $changed 个文件内容不一致" }
Write-Host "   内容校验：$($srcFiles.Count) 个文件与 server/python 完全一致"

# ---------------------------------------------------------------- 3. 找解释器
if (-not $Python) {
    foreach ($candidate in @(
            $env:AUTOSYNC_PYTHON,
            (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            "python")) {
        if ($candidate -and (($candidate -eq "python") -or (Test-Path $candidate))) { $Python = $candidate; break }
    }
}
Write-Host "   解释器     : $Python"
$env:PYTHONIOENCODING = "utf-8"

# ---------------------------------------------------------------- 4. 校验入口
if (-not $SkipVerify) {
    Write-Host "   校验入口（tools/verify-mcdr-entry.py）..."
    & $Python (Join-Path $RepoRoot "tools\verify-mcdr-entry.py")
    if ($LASTEXITCODE -ne 0) { throw "入口校验失败（退出码 $LASTEXITCODE），已中止打包" }
}

# ---------------------------------------------------------------- 5. 打包
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
try {
    Copy-Item $McdrPkg (Join-Path $Staging "autosync") -Recurse -Force
    Copy-Item $PluginJson (Join-Path $Staging "mcdreforged.plugin.json") -Force
    if (Test-Path $ConfigExample) { Copy-Item $ConfigExample (Join-Path $Staging "config.example.json") -Force }

    Get-ChildItem $Staging -Recurse -Directory -Filter "__pycache__" |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force }

    $zip = Join-Path ([System.IO.Path]::GetTempPath()) ("autosync-mcdr-" + [System.Guid]::NewGuid().ToString("N") + ".zip")
    Compress-Archive -Path (Join-Path $Staging "*") -DestinationPath $zip -CompressionLevel Optimal -Force

    if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
    Move-Item $zip $OutFile -Force
}
finally {
    if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
}

$size = (Get-Item $OutFile).Length
Write-Host ("   已生成：{0}  ({1:N0} 字节 / {2:N1} KB)" -f $OutFile, $size, ($size / 1KB))

# ---------------------------------------------------------------- 6. 检查包结构
Write-Host "   检查包结构（tools/check-mcdr-package.py）..."
& $Python (Join-Path $RepoRoot "tools\check-mcdr-package.py") $OutFile
if ($LASTEXITCODE -ne 0) { throw "包结构检查失败（退出码 $LASTEXITCODE）" }

# 打印包内结构，方便肉眼确认
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($OutFile)
try {
    Write-Host "   包内文件："
    $archive.Entries | Sort-Object FullName | ForEach-Object {
        Write-Host ("     {0,-40} {1,8:N0} B" -f $_.FullName, $_.Length)
    }
}
finally {
    $archive.Dispose()
}

Write-Host "== 完成。把 $OutFile 放进 MCDR 的 plugins/ 目录即可。"
