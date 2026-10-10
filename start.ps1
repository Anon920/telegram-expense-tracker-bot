$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if ($pythonCommand) { $botPython = $pythonCommand.Source }
elseif (Test-Path -LiteralPath $bundledPython) { $botPython = $bundledPython }
else { throw 'Install Python 3.11 or newer from python.org (Add Python to PATH).' }
& $botPython -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')"
if ($LASTEXITCODE -ne 0) {
    & $botPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install tzdata.' }
}
& $botPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed. The bot was not started.' }
$configPath = Join-Path $PSScriptRoot 'config.json'
if (-not $env:TELEGRAM_BOT_TOKEN -and -not (Test-Path -LiteralPath $configPath)) {
    $secretToken = Read-Host 'BotFather token (hidden input; saved to config.json)' -AsSecureString
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretToken)
    try {
        $configText = @{ telegram_bot_token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer) } | ConvertTo-Json
        [System.IO.File]::WriteAllText($configPath, $configText, [System.Text.UTF8Encoding]::new($false))
        $configText = $null
    }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer) }
}
try { & $botPython bot.py }
finally { Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue }
