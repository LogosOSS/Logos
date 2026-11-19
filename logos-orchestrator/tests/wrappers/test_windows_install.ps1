param([string]$CoveragePath = 'logos-ui/coverage/wrappers.xml')

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$wrapper = Join-Path $repo 'logos-ui/public/claude-logos.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('logos-wrapper-' + [Guid]::NewGuid())
$originalConfigDir = $env:LOGOS_CONFIG_DIR
$originalInstallDir = $env:LOGOS_INSTALL_DIR
$env:LOGOS_CONFIG_DIR = Join-Path $temp 'config'
$env:LOGOS_INSTALL_DIR = Join-Path $temp "install with spaces and ' quote"

function Assert-Equal($Actual, $Expected, [string]$Message) {
    if ($Actual -cne $Expected) { throw "$Message`nExpected: $Expected`nActual: $Actual" }
}

# Record actual execution with the built-in debugger, including uncovered
# statements. This works in Windows PowerShell 5.1 without a test dependency.
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($wrapper, [ref]$null, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$lines = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.StatementAst] -and
        $node -isnot [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true) | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object -Unique)
$breakpoints = Set-PSBreakpoint -Script $wrapper -Line $lines -Action { continue }

try {
    $model = 'model with spaces and $literal'
    $config = "LOGOS_URL=https://example.test/`nLOGOS_MODEL=$model`nLOGOS_KEY=test-placeholder`n"
    & $wrapper -Install -LogosConfig $config
    if ($LASTEXITCODE -ne 0) { throw "Installation exited with $LASTEXITCODE" }

    $installed = Join-Path $env:LOGOS_INSTALL_DIR 'claude-logos.ps1'
    Assert-Equal (Get-FileHash -LiteralPath $installed).Hash (Get-FileHash -LiteralPath $wrapper).Hash 'Wrapper content changed during installation'
    Assert-Equal (Get-Content -Raw -LiteralPath (Join-Path $env:LOGOS_CONFIG_DIR 'key')) 'test-placeholder' 'Key was not preserved'
    $written = Get-Content -LiteralPath (Join-Path $env:LOGOS_CONFIG_DIR 'config')
    if ($written -cnotcontains 'LOGOS_URL=https://example.test') { throw 'URL was not normalized' }
    if ($written -cnotcontains "LOGOS_MODEL=$model") { throw 'Model was not preserved literally' }
    $shim = Get-Content -Raw -LiteralPath (Join-Path $env:LOGOS_INSTALL_DIR 'claude-logos.cmd')
    if (-not $shim.Contains('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-logos.ps1" %*')) {
        throw 'Command shim does not invoke the installed wrapper'
    }

    # Reinstall with changed configuration to exercise replacement, not just copy.
    & $wrapper -Install -LogosConfig "LOGOS_URL=https://other.example.test`nLOGOS_KEY=replacement-key`n"
    if ($LASTEXITCODE -ne 0) { throw "Reinstallation exited with $LASTEXITCODE" }
    Assert-Equal (Get-Content -Raw -LiteralPath (Join-Path $env:LOGOS_CONFIG_DIR 'key')) 'replacement-key' 'Reinstallation did not replace the key'
    if ((Get-Content -LiteralPath (Join-Path $env:LOGOS_CONFIG_DIR 'config')) -match '^LOGOS_MODEL=') {
        throw 'Reinstallation retained an obsolete model'
    }

    $revisionLine = (Select-String -LiteralPath $wrapper -Pattern '^\$ClaudeLogosVersion\s*=').LineNumber
    if (-not ($breakpoints | Where-Object { $_.Line -eq $revisionLine -and $_.HitCount -gt 0 })) {
        throw 'The coverage collector did not observe wrapper initialization'
    }
    if (-not ($breakpoints | Where-Object { $_.HitCount -eq 0 })) {
        throw 'The coverage collector must retain unexecuted statements'
    }

    $report = Join-Path $repo $CoveragePath
    New-Item -ItemType Directory -Force -Path (Split-Path $report) | Out-Null
    $xml = New-Object System.Xml.XmlDocument
    $coverage = $xml.CreateElement('coverage')
    $coverage.SetAttribute('version', '1')
    $xml.AppendChild($coverage) | Out-Null
    $file = $xml.CreateElement('file')
    $file.SetAttribute('path', 'logos-ui/public/claude-logos.ps1')
    $coverage.AppendChild($file) | Out-Null
    foreach ($point in $breakpoints) {
        $line = $xml.CreateElement('lineToCover')
        $line.SetAttribute('lineNumber', [string]$point.Line)
        $line.SetAttribute('covered', ([string]($point.HitCount -gt 0)).ToLowerInvariant())
        $file.AppendChild($line) | Out-Null
    }
    $xml.Save($report)
    Write-Host "PowerShell installation and reinstallation passed; coverage: $report"
} finally {
    $breakpoints | Remove-PSBreakpoint
    $env:LOGOS_CONFIG_DIR = $originalConfigDir
    $env:LOGOS_INSTALL_DIR = $originalInstallDir
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
