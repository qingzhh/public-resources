param(
    [string]$OutputDir = ".\dist"
)

$ErrorActionPreference = "Stop"

$packageName = "u2-qb-bot-share-v2.1"
$packageRoot = Join-Path $OutputDir $packageName
$zipPath = Join-Path $OutputDir ($packageName + ".zip")

if (Test-Path $packageRoot) {
    Remove-Item -Path $packageRoot -Recurse -Force
}

if (Test-Path $zipPath) {
    Remove-Item -Path $zipPath -Force
}

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

$files = @(
    "README.md",
    "u2_qb_bot.sh",
    "u2_manager.py",
    "u2_service.sh",
    "install_systemd.sh",
    "deploy_vps_v2.1.ps1",
    "deploy.template.jsonc",
    "config.template.properties",
    "DEPLOY_GUIDE_zh-CN.md"
)

foreach ($file in $files) {
    Copy-Item -Path (Join-Path $PSScriptRoot $file) -Destination $packageRoot -Force
}

New-Item -ItemType Directory -Path (Join-Path $packageRoot "scripts") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "scripts\\install_u2_qb_bot.sh") -Destination (Join-Path $packageRoot "scripts") -Force

$extraFiles = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter *.md

foreach ($file in $extraFiles) {
    Copy-Item -LiteralPath $file.FullName -Destination $packageRoot -Force
}

Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $zipPath -Force

Write-Host "Package folder: $packageRoot"
Write-Host "Package zip: $zipPath"
