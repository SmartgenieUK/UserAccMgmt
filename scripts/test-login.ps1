param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$Email,
    [string]$Password
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-JsonRequest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [object]$Body,
        [hashtable]$Headers
    )

    $params = @{
        Uri                = $Uri
        Method             = $Method
        UseBasicParsing    = $true
        SkipHttpErrorCheck = $true
        TimeoutSec         = 30
    }
    if ($Headers) {
        $params["Headers"] = $Headers
    }
    if ($Body) {
        $params["ContentType"] = "application/json"
        $params["Body"] = ($Body | ConvertTo-Json -Depth 8)
    }

    $resp = Invoke-WebRequest @params
    $parsed = $null
    try {
        $parsed = $resp.Content | ConvertFrom-Json -ErrorAction Stop
    } catch {
        $parsed = $resp.Content
    }
    return [PSCustomObject]@{
        Status = [int]$resp.StatusCode
        Body   = $parsed
        Raw    = $resp.Content
    }
}

Write-Host "Checking health endpoints at $BaseUrl ..."
$health = Invoke-JsonRequest -Method GET -Uri "$BaseUrl/api/v1/health"
$ready = Invoke-JsonRequest -Method GET -Uri "$BaseUrl/api/v1/ready"
Write-Host "health: $($health.Status) $($health.Raw)"
Write-Host "ready:  $($ready.Status) $($ready.Raw)"

if ($health.Status -ne 200 -or $ready.Status -ne 200) {
    throw "Service is not healthy/ready."
}

Write-Host "Checking login page ..."
$loginPage = Invoke-WebRequest -Uri "$BaseUrl/login" -UseBasicParsing -TimeoutSec 30
$hasGoogle = $loginPage.Content -like "*Continue with Google*"
$hasMicrosoft = $loginPage.Content -like "*Continue with Microsoft*"
Write-Host "login page: $($loginPage.StatusCode)"
Write-Host "google button present: $hasGoogle"
Write-Host "microsoft button present: $hasMicrosoft"

if (-not $Email -or -not $Password) {
    Write-Host "No -Email/-Password provided. Skipping credential login test."
    exit 0
}

Write-Host "Testing password login for $Email ..."
$login = Invoke-JsonRequest -Method POST -Uri "$BaseUrl/api/v1/login" -Body @{
    email    = $Email
    password = $Password
}

Write-Host "login status: $($login.Status)"
Write-Host "login body: $($login.Raw)"

if ($login.Status -ne 200) {
    if ($login.Raw -like "*email_not_verified*") {
        Write-Host "Login blocked because email is not verified (expected policy)."
        exit 0
    }
    throw "Login failed."
}

$token = $login.Body.access_token
if (-not $token) {
    throw "Login succeeded but no access token found."
}

Write-Host "Testing /me with bearer token ..."
$me = Invoke-JsonRequest -Method GET -Uri "$BaseUrl/api/v1/me" -Headers @{
    Authorization = "Bearer $token"
}
Write-Host "me status: $($me.Status)"
Write-Host "me body: $($me.Raw)"

if ($me.Status -ne 200) {
    throw "/me request failed."
}

Write-Host "Login flow test passed."
