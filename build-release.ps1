param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ApiBase,
    [string]$OutputDir = "dist",
    [switch]$AllowLocalHttp,
    [ValidatePattern("^[a-zA-Z0-9._-]*$")]
    [string]$PackageSuffix = ""
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptRoot

function Copy-ProjectFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $targetDir = Split-Path -Parent $Destination
    if ($targetDir -and -not (Test-Path -LiteralPath $targetDir -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDir | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

$apiUri = $null
if (-not [Uri]::TryCreate($ApiBase, [UriKind]::Absolute, [ref]$apiUri) -or
    -not $apiUri.Host -or
    $apiUri.UserInfo -or
    $apiUri.Query -or
    $apiUri.Fragment) {
    throw "-ApiBase must be a valid URL without credentials, query, or fragment."
}
$isHttps = $apiUri.Scheme -eq "https"
$isAllowedLocalHttp = $AllowLocalHttp -and
    $apiUri.Scheme -eq "http" -and
    $apiUri.Host -in @("127.0.0.1", "localhost", "::1")
if (-not $isHttps -and -not $isAllowedLocalHttp) {
    throw "-ApiBase must use HTTPS. For local testing only, pass -AllowLocalHttp with localhost or 127.0.0.1."
}
$normalizedApiBase = $apiUri.AbsoluteUri.TrimEnd("/")
$apiHostPermission = $apiUri.GetLeftPart([UriPartial]::Authority).TrimEnd("/") + "/*"

$manifestPath = Join-Path $scriptRoot "browser-extension\manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Missing browser-extension\manifest.json."
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$version = if ($manifest.version) { [string]$manifest.version } else { "dev" }
$packageName = "sing-reactor-extension-$version"
if ($PackageSuffix) {
    $packageName += "-$PackageSuffix"
}
$distRoot = Join-Path $scriptRoot $OutputDir
$extensionRoot = Join-Path $distRoot $packageName
$extensionZip = Join-Path $distRoot "$packageName.zip"

if (Test-Path -LiteralPath $extensionRoot) {
    Remove-Item -LiteralPath $extensionRoot -Recurse -Force
}
if (Test-Path -LiteralPath $extensionZip -PathType Leaf) {
    Remove-Item -LiteralPath $extensionZip -Force
}
New-Item -ItemType Directory -Path $extensionRoot -Force | Out-Null

$extensionFiles = @(
    "background.js",
    "content.css",
    "content.js",
    "logo.png",
    "popup.css",
    "popup.html",
    "protocol.js",
    "storage.js"
)
foreach ($file in $extensionFiles) {
    $source = Join-Path $scriptRoot "browser-extension\$file"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Missing extension release file: browser-extension\$file"
    }
    Copy-ProjectFile -Source $source -Destination (Join-Path $extensionRoot $file)
}

Copy-ProjectFile -Source (Join-Path $scriptRoot "INSTALL.md") -Destination (Join-Path $extensionRoot "INSTALL.md")

$manifest.host_permissions = @(
    "https://api.bilibili.com/*",
    "https://*.hdslb.com/*",
    $apiHostPermission
)
$manifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $extensionRoot "manifest.json") -Encoding UTF8

$configJson = $normalizedApiBase | ConvertTo-Json -Compress
$configContent = @"
(() => {
  'use strict';

  globalThis.BLSConfig = Object.freeze({
    API_BASE: $configJson
  });
})();
"@
Set-Content -LiteralPath (Join-Path $extensionRoot "config.js") -Value $configContent -Encoding UTF8

$filesToZip = Get-ChildItem -LiteralPath $extensionRoot -File -Force | Select-Object -ExpandProperty FullName
Compress-Archive -LiteralPath $filesToZip -DestinationPath $extensionZip -Force

Write-Host "Browser extension directory: $extensionRoot" -ForegroundColor Green
Write-Host "Browser extension ZIP: $extensionZip" -ForegroundColor Green
