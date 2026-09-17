# 从全局安装目录刷新本仓库里的 skill 副本(单向, 全局 -> 仓库)
$src = Join-Path $env:USERPROFILE ".omp\agent\skills\AutoCAD"
$dst = Join-Path $PSScriptRoot "skills\autocad-drawing"

if (-not (Test-Path $src)) { Write-Error "找不到全局安装目录: $src"; exit 1 }

robocopy $src $dst /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Error "robocopy 失败, exit=$LASTEXITCODE"; exit 1 }

Write-Output "synced: $src"
Write-Output "     -> $dst"
Get-ChildItem -Recurse -File $dst | ForEach-Object {
    Write-Output ("  {0}  {1} bytes" -f $_.FullName.Substring($dst.Length + 1), $_.Length)
}