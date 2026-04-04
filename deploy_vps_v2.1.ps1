param(
    [string]$ConfigFile = ".\deploy.jsonc"
)

$ErrorActionPreference = "Stop"

function Ensure-Module {
    param([string]$Name)

    if (-not (Get-Module -ListAvailable -Name $Name)) {
        Install-PackageProvider -Name NuGet -Force -Scope CurrentUser | Out-Null
        Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
        Install-Module -Name $Name -Force -Scope CurrentUser
    }

    Import-Module $Name -Force
}

function New-Utf8NoBomFile {
    param(
        [string]$Path,
        [string]$Content
    )

    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Read-DeployConfig {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Config file not found: $Path"
    }

    $raw = Get-Content -Path $Path
    $cleanLines = foreach ($line in $raw) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("//") -or $trimmed.StartsWith("#")) {
            continue
        }
        $line
    }

    $json = ($cleanLines -join "`n").Trim()
    if (-not $json) {
        throw "Config file is empty after removing comments: $Path"
    }

    return $json | ConvertFrom-Json
}

function Assert-NotBlank {
    param(
        [string]$Value,
        [string]$FieldName
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Missing required value: $FieldName"
    }
}

$config = Read-DeployConfig -Path $ConfigFile

Assert-NotBlank -Value $config.ssh.host -FieldName "ssh.host"
Assert-NotBlank -Value ([string]$config.ssh.port) -FieldName "ssh.port"
Assert-NotBlank -Value $config.ssh.user -FieldName "ssh.user"
Assert-NotBlank -Value $config.ssh.password -FieldName "ssh.password"
Assert-NotBlank -Value $config.install_dir -FieldName "install_dir"
Assert-NotBlank -Value $config.service_name -FieldName "service_name"
Assert-NotBlank -Value $config.u2.passkey -FieldName "u2.passkey"
Assert-NotBlank -Value $config.u2.cookiecloud.url -FieldName "u2.cookiecloud.url"
Assert-NotBlank -Value $config.u2.cookiecloud.key -FieldName "u2.cookiecloud.key"
Assert-NotBlank -Value $config.u2.cookiecloud.password -FieldName "u2.cookiecloud.password"
Assert-NotBlank -Value $config.qb.url -FieldName "qb.url"
Assert-NotBlank -Value $config.qb.user -FieldName "qb.user"
Assert-NotBlank -Value $config.qb.pass -FieldName "qb.pass"
Assert-NotBlank -Value $config.qb.category -FieldName "qb.category"
Assert-NotBlank -Value ([string]$config.qb.up_limit_mb) -FieldName "qb.up_limit_mb"
Assert-NotBlank -Value ([string]$config.qb.max_downloading) -FieldName "qb.max_downloading"
Assert-NotBlank -Value ([string]$config.bot.poll_interval) -FieldName "bot.poll_interval"

$qbSessionRefreshSeconds = if (
    $null -ne $config.qb.PSObject.Properties["session_refresh_seconds"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.qb.session_refresh_seconds)
) {
    [string]$config.qb.session_refresh_seconds
} else {
    "1500"
}

$qbMinFreeSpaceGb = if (
    $null -ne $config.qb.PSObject.Properties["min_free_space_gb"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.qb.min_free_space_gb)
) {
    [string]$config.qb.min_free_space_gb
} else {
    "0"
}

$seedReaddCooldownMinutes = if (
    $null -ne $config.PSObject.Properties["seed"] -and
    $null -ne $config.seed.PSObject.Properties["readd_cooldown_minutes"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.seed.readd_cooldown_minutes)
) {
    [string]$config.seed.readd_cooldown_minutes
} else {
    "1440"
}

$shoutMaxAgeMinutes = if (
    $null -ne $config.PSObject.Properties["shout"] -and
    $null -ne $config.shout.PSObject.Properties["max_age_minutes"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.shout.max_age_minutes)
) {
    [string]$config.shout.max_age_minutes
} else {
    "120"
}

$magicUpRates = if (
    $null -ne $config.PSObject.Properties["magic"] -and
    $null -ne $config.magic.PSObject.Properties["up_rates"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.magic.up_rates)
) {
    [string]$config.magic.up_rates
} else {
    "1.00,2.00,2.33"
}

$magicUseThresholds = if (
    $null -ne $config.PSObject.Properties["magic"] -and
    $null -ne $config.magic.PSObject.Properties["use_thresholds"]
) {
    if ([bool]$config.magic.use_thresholds) { "1" } else { "0" }
} else {
    "0"
}

$magicMinUpRate = if (
    $null -ne $config.PSObject.Properties["magic"] -and
    $null -ne $config.magic.PSObject.Properties["min_up_rate"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.magic.min_up_rate)
) {
    [string]$config.magic.min_up_rate
} else {
    "1.00"
}

$magicMaxDownRate = if (
    $null -ne $config.PSObject.Properties["magic"] -and
    $null -ne $config.magic.PSObject.Properties["max_down_rate"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.magic.max_down_rate)
) {
    [string]$config.magic.max_down_rate
} else {
    "0.00"
}

function Get-DynamicValue {
    param(
        $ConfigRoot,
        [string]$RuleName,
        [string]$FieldName,
        [string]$DefaultValue
    )

    if (
        $null -ne $ConfigRoot.PSObject.Properties["dynamic"] -and
        $null -ne $ConfigRoot.dynamic.PSObject.Properties[$RuleName] -and
        $null -ne $ConfigRoot.dynamic.$RuleName.PSObject.Properties[$FieldName] -and
        -not [string]::IsNullOrWhiteSpace([string]$ConfigRoot.dynamic.$RuleName.$FieldName)
    ) {
        return [string]$ConfigRoot.dynamic.$RuleName.$FieldName
    }

    return $DefaultValue
}

$dynamicRule1Enabled = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "enabled" -DefaultValue "False"
$dynamicRule1FreeSpace = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "free_space_le_gb" -DefaultValue "0"
$dynamicRule1MinSize = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "min_size_gb" -DefaultValue "0"
$dynamicRule1MaxSize = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "max_size_gb" -DefaultValue "0"
$dynamicRule1MinUp = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "min_up_rate" -DefaultValue "0"
$dynamicRule1MaxDown = Get-DynamicValue -ConfigRoot $config -RuleName "rule1" -FieldName "max_down_rate" -DefaultValue "999"

$dynamicRule2Enabled = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "enabled" -DefaultValue "False"
$dynamicRule2FreeSpace = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "free_space_le_gb" -DefaultValue "0"
$dynamicRule2MinSize = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "min_size_gb" -DefaultValue "0"
$dynamicRule2MaxSize = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "max_size_gb" -DefaultValue "0"
$dynamicRule2MinUp = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "min_up_rate" -DefaultValue "0"
$dynamicRule2MaxDown = Get-DynamicValue -ConfigRoot $config -RuleName "rule2" -FieldName "max_down_rate" -DefaultValue "999"

$webHost = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["host"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.host)
) {
    [string]$config.web.host
} else {
    "0.0.0.0"
}

$webPort = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["port"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.port)
) {
    [string]$config.web.port
} else {
    "18081"
}

$webToken = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["token"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.token)
) {
    [string]$config.web.token
} else {
    ""
}

$webUsername = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["username"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.username)
) {
    [string]$config.web.username
} else {
    "admin"
}

$webPassword = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["password"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.password)
) {
    [string]$config.web.password
} else {
    $webToken
}

$webSessionHours = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["session_hours"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.session_hours)
) {
    [string]$config.web.session_hours
} else {
    "12"
}

$webQbRefreshSeconds = if (
    $null -ne $config.PSObject.Properties["web"] -and
    $null -ne $config.web.PSObject.Properties["qb_refresh_seconds"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.web.qb_refresh_seconds)
) {
    [string]$config.web.qb_refresh_seconds
} else {
    "5"
}

$logArchiveIntervalMinutes = if (
    $null -ne $config.PSObject.Properties["log"] -and
    $null -ne $config.log.PSObject.Properties["archive_interval_minutes"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.log.archive_interval_minutes)
) {
    [string]$config.log.archive_interval_minutes
} else {
    "60"
}

$logArchiveKeepCount = if (
    $null -ne $config.PSObject.Properties["log"] -and
    $null -ne $config.log.PSObject.Properties["archive_keep_count"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.log.archive_keep_count)
) {
    [string]$config.log.archive_keep_count
} else {
    "20"
}

$telegramBotToken = if (
    $null -ne $config.PSObject.Properties["telegram"] -and
    $null -ne $config.telegram.PSObject.Properties["bot_token"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.telegram.bot_token)
) {
    [string]$config.telegram.bot_token
} else {
    ""
}

$telegramChatId = if (
    $null -ne $config.PSObject.Properties["telegram"] -and
    $null -ne $config.telegram.PSObject.Properties["chat_id"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.telegram.chat_id)
) {
    [string]$config.telegram.chat_id
} else {
    ""
}

$telegramPollSeconds = if (
    $null -ne $config.PSObject.Properties["telegram"] -and
    $null -ne $config.telegram.PSObject.Properties["poll_seconds"] -and
    -not [string]::IsNullOrWhiteSpace([string]$config.telegram.poll_seconds)
) {
    [string]$config.telegram.poll_seconds
} else {
    "3"
}

Ensure-Module -Name Posh-SSH

$sshPassword = ConvertTo-SecureString $config.ssh.password -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($config.ssh.user, $sshPassword)

$installDir = $config.install_dir
$serviceName = $config.service_name

$configBody = @"
# ===== U2 =====
# Prefer CookieCloud so the VPS can reuse your latest browser login state.
cookiecloud.url=$($config.u2.cookiecloud.url)
cookiecloud.key=$($config.u2.cookiecloud.key)
cookiecloud.password=$($config.u2.cookiecloud.password)

# Optional fallback: paste a raw browser Cookie header here.
cookie=

# Legacy fallback: cookies.txt or one-line cookie file.
cookie_file=
passkey=$($config.u2.passkey)

# ===== QB =====
qb.url=$($config.qb.url)
qb.user=$($config.qb.user)
qb.pass=$($config.qb.pass)

# ===== QB Torrent Settings =====
qb.category=$($config.qb.category)

# Upload speed limit in MB/s.
# 0 = unlimited
qb.up_limit_mb=$($config.qb.up_limit_mb)

# Max currently-downloading torrents.
# 0 = unlimited
qb.max_downloading=$($config.qb.max_downloading)

# Minimum safe free space in GB before adding new torrents.
# 0 = disabled
qb.min_free_space_gb=$qbMinFreeSpaceGb

# Re-add cooldown in minutes after a successful import.
# 0 = disabled
seed.readd_cooldown_minutes=$seedReaddCooldownMinutes

# Ignore shout messages older than this many minutes.
# 0 = disabled
shout.max_age_minutes=$shoutMaxAgeMinutes

# Allowed upload magic rates.
magic.up_rates=$magicUpRates
magic.use_thresholds=$magicUseThresholds
magic.min_up_rate=$magicMinUpRate
magic.max_down_rate=$magicMaxDownRate
dynamic.rule1.enabled=$dynamicRule1Enabled
dynamic.rule1.free_space_le_gb=$dynamicRule1FreeSpace
dynamic.rule1.min_size_gb=$dynamicRule1MinSize
dynamic.rule1.max_size_gb=$dynamicRule1MaxSize
dynamic.rule1.min_up_rate=$dynamicRule1MinUp
dynamic.rule1.max_down_rate=$dynamicRule1MaxDown
dynamic.rule2.enabled=$dynamicRule2Enabled
dynamic.rule2.free_space_le_gb=$dynamicRule2FreeSpace
dynamic.rule2.min_size_gb=$dynamicRule2MinSize
dynamic.rule2.max_size_gb=$dynamicRule2MaxSize
dynamic.rule2.min_up_rate=$dynamicRule2MinUp
dynamic.rule2.max_down_rate=$dynamicRule2MaxDown

# Proactively refresh qB session before it gets too old.
# 0 = disabled
qb.session_refresh_seconds=$qbSessionRefreshSeconds

# ===== Web UI =====
web.host=$webHost
web.port=$webPort
web.username=$webUsername
web.password=$webPassword
web.session_hours=$webSessionHours
web.qb_refresh_seconds=$webQbRefreshSeconds
log.archive_interval_minutes=$logArchiveIntervalMinutes
log.archive_keep_count=$logArchiveKeepCount
web.token=$webToken

# ===== Telegram =====
telegram.bot_token=$telegramBotToken
telegram.chat_id=$telegramChatId
telegram.poll_seconds=$telegramPollSeconds

# Poll interval in seconds.
poll_interval=$($config.bot.poll_interval)
"@

$tempDir = Join-Path $env:TEMP ("u2-qb-bot-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $tempDir | Out-Null

$localConfig = Join-Path $tempDir "config.properties"
New-Utf8NoBomFile -Path $localConfig -Content $configBody

$sshSession = $null
$sftpSession = $null

try {
    $sshSession = New-SSHSession -ComputerName $config.ssh.host -Port $config.ssh.port -Credential $credential -AcceptKey -ConnectionTimeout 20
    $sftpSession = New-SFTPSession -ComputerName $config.ssh.host -Port $config.ssh.port -Credential $credential -AcceptKey -ConnectionTimeout 20

    Invoke-SSHCommand -SessionId $sshSession.SessionId -Command "mkdir -p '$installDir'" | Out-Null

    Set-SFTPItem -SessionId $sftpSession.SessionId -Path (Join-Path $PSScriptRoot "u2_qb_bot.sh") -Destination $installDir -Force
    Set-SFTPItem -SessionId $sftpSession.SessionId -Path (Join-Path $PSScriptRoot "u2_manager.py") -Destination $installDir -Force
    Set-SFTPItem -SessionId $sftpSession.SessionId -Path (Join-Path $PSScriptRoot "u2_service.sh") -Destination $installDir -Force
    Set-SFTPItem -SessionId $sftpSession.SessionId -Path (Join-Path $PSScriptRoot "install_systemd.sh") -Destination $installDir -Force
    Set-SFTPItem -SessionId $sftpSession.SessionId -Path $localConfig -Destination $installDir -Force
    Set-SFTPItem -SessionId $sftpSession.SessionId -Path $ConfigFile -Destination $installDir -Force

    $configFileName = Split-Path -Leaf $ConfigFile
    if ($configFileName -ne "deploy.jsonc") {
        Invoke-SSHCommand -SessionId $sshSession.SessionId -Command "cd '$installDir' && mv -f '$configFileName' 'deploy.jsonc'" | Out-Null
    }

    $remoteCommand = @"
cd '$installDir'
chmod +x u2_qb_bot.sh u2_service.sh install_systemd.sh
./u2_service.sh stop >/dev/null 2>&1 || true
./u2_service.sh web-stop >/dev/null 2>&1 || true
./u2_service.sh telegram-stop >/dev/null 2>&1 || true
bash ./install_systemd.sh '$installDir' '$serviceName'
sleep 3
./u2_service.sh status
./u2_service.sh web-status
./u2_service.sh telegram-status
printf '\n===== health-check =====\n'
./u2_service.sh check || true
printf '\n===== u2.log =====\n'
tail -n 80 u2.log
"@

    Invoke-SSHCommand -SessionId $sshSession.SessionId -Command $remoteCommand | Select-Object -ExpandProperty Output
}
finally {
    if ($sftpSession) {
        Remove-SFTPSession -SessionId $sftpSession.SessionId | Out-Null
    }
    if ($sshSession) {
        Remove-SSHSession -SessionId $sshSession.SessionId | Out-Null
    }
    if (Test-Path $tempDir) {
        Remove-Item -Path $tempDir -Recurse -Force
    }
}
