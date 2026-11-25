param([string]$DistPath = '发布')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$model = 'zipvoice-distill-fp32'
$buildArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
    '--name', '听页', '--specpath', 'build', '--workpath', 'build/work',
    '--distpath', $DistPath, '--collect-binaries', 'sherpa_onnx'
)
foreach ($name in @('text_encoder.onnx', 'fm_decoder.onnx', 'lexicon.txt', 'tokens.txt')) {
    $buildArgs += @('--add-data', "${PSScriptRoot}/models/$model/${name};models/$model")
}
foreach ($name in @('vocos_24khz.onnx', 'lexicon-en-us.txt', 'reader-reference-distill-zh.wav')) {
    $buildArgs += @('--add-data', "${PSScriptRoot}/models/${name};models")
}
& $python @buildArgs 'reader.pyw'
exit $LASTEXITCODE
