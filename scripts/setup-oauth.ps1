param(
    [string]$EnvFile = ".env",
    [string]$GoogleClientId,
    [string]$GoogleClientSecret,
    [string]$GoogleRedirectUri = "http://localhost:8000/login/oauth/google/callback",
    [string]$MicrosoftClientId,
    [string]$MicrosoftClientSecret,
    [string]$MicrosoftRedirectUri = "http://localhost:8000/login/oauth/microsoft/callback",
    [switch]$CreateMicrosoftApp,
    [string]$MicrosoftAppDisplayName = "UserAccMgmt-Local",
    [string]$TenantId,
    [switch]$RestartContainers
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $escapedKey = [Regex]::Escape($Key)
    $line = "$Key=$Value"
    if ($Content -match "(?m)^$escapedKey=") {
        return [Regex]::Replace($Content, "(?m)^$escapedKey=.*$", $line)
    }
    if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) {
        $Content += "`r`n"
    }
    return $Content + $line + "`r`n"
}

if (-not (Test-Path -Path $EnvFile)) {
    throw "Env file not found: $EnvFile"
}

if ($CreateMicrosoftApp) {
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "Azure CLI (az) is required for -CreateMicrosoftApp."
    }

    if ($TenantId) {
        az login --tenant $TenantId --allow-no-subscriptions | Out-Null
    } else {
        az login --allow-no-subscriptions | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Azure login failed."
    }

    $appJson = az ad app create `
        --display-name $MicrosoftAppDisplayName `
        --sign-in-audience AzureADandPersonalMicrosoftAccount `
        --web-redirect-uris $MicrosoftRedirectUri `
        --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Microsoft app registration. Ensure you have Entra permissions (Application Administrator / Cloud Application Administrator / Global Administrator). Details: $appJson"
    }

    $app = $appJson | ConvertFrom-Json
    if (-not $app.appId) {
        throw "Failed to create Microsoft app registration."
    }

    $secret = az ad app credential reset `
        --id $app.appId `
        --display-name "local-dev" `
        --years 1 `
        --query password `
        --output tsv 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Microsoft app client secret. Details: $secret"
    }

    if (-not $secret) {
        throw "Failed to create Microsoft app client secret."
    }

    $MicrosoftClientId = $app.appId
    $MicrosoftClientSecret = $secret
    Write-Host "Created Microsoft app registration: $MicrosoftAppDisplayName"
    Write-Host "MICROSOFT_CLIENT_ID: $MicrosoftClientId"
    Write-Host "MICROSOFT_CLIENT_SECRET: $MicrosoftClientSecret"
}

$content = Get-Content -Path $EnvFile -Raw

if ($GoogleClientId) {
    $content = Set-EnvValue -Content $content -Key "GOOGLE_CLIENT_ID" -Value $GoogleClientId
}
if ($GoogleClientSecret) {
    $content = Set-EnvValue -Content $content -Key "GOOGLE_CLIENT_SECRET" -Value $GoogleClientSecret
}
if ($GoogleRedirectUri) {
    $content = Set-EnvValue -Content $content -Key "GOOGLE_REDIRECT_URI" -Value $GoogleRedirectUri
}
if ($MicrosoftClientId) {
    $content = Set-EnvValue -Content $content -Key "MICROSOFT_CLIENT_ID" -Value $MicrosoftClientId
}
if ($MicrosoftClientSecret) {
    $content = Set-EnvValue -Content $content -Key "MICROSOFT_CLIENT_SECRET" -Value $MicrosoftClientSecret
}
if ($MicrosoftRedirectUri) {
    $content = Set-EnvValue -Content $content -Key "MICROSOFT_REDIRECT_URI" -Value $MicrosoftRedirectUri
}

Set-Content -Path $EnvFile -Value $content -Encoding UTF8
Write-Host "Updated OAuth values in $EnvFile"

if ($RestartContainers) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required for -RestartContainers."
    }
    docker compose up -d --build
    Write-Host "Containers restarted."
}
