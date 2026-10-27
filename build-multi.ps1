$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$previousDirectory = Get-Location
try {
    Set-Location -LiteralPath $projectDirectory
    if (-not (Test-Path -LiteralPath '.venv-multi/Scripts/python.exe')) {
        python -m venv .venv-multi
        if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
    }
    $runtimePython = Join-Path $projectDirectory '.venv-multi/Scripts/python.exe'
    & $runtimePython -m pip install -r requirements-multi-lock.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
    $staticDirectory = Join-Path $projectDirectory 'backend/app/local_multi/static'
    & $runtimePython -m PyInstaller --noconfirm --onedir --windowed --name ZhidaoMulti --paths backend --add-data "$staticDirectory;app/local_multi/static" --collect-submodules uvicorn --collect-all playwright backend/run_multi.py
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    Write-Output '构建完成：dist/ZhidaoMulti/ZhidaoMulti.exe'
} finally {
    Set-Location -LiteralPath $previousDirectory
}
