param([string]$DistPath = '发布/程序/对白自适应版')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$buildArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
    '--name', '听页', '--specpath', 'build', '--workpath', 'build/work',
    '--distpath', $DistPath, '--collect-binaries', 'sherpa_onnx',
    '--additional-hooks-dir', 'packaging/hooks'
)
& $python @buildArgs 'reader.pyw'
exit $LASTEXITCODE
