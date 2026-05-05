param(
    [string]$ConfigPath = "$HOME\KIS\config\kis_devlp.yaml"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-Required {
    param([string]$Prompt)
    do {
        $value = Read-Host $Prompt
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value.Trim()
        }
        Write-Host "Required value. Please enter again." -ForegroundColor Yellow
    } while ($true)
}

function Read-SecretPlain {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Quote-Yaml {
    param([string]$Value)
    if ($null -eq $Value) { $Value = "" }
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

Write-Host ""
Write-Host "KIS Open API local config setup" -ForegroundColor Cyan
Write-Host "Values are written only to: $ConfigPath"
Write-Host "Do not paste keys into chat or commit this file."
Write-Host ""

$envChoice = Read-Host "Environment [vps=paper/mock, prod=real] (default: vps)"
if ([string]::IsNullOrWhiteSpace($envChoice)) { $envChoice = "vps" }
$envChoice = $envChoice.Trim().ToLowerInvariant()
if ($envChoice -notin @("vps", "prod")) {
    throw "Environment must be 'vps' or 'prod'."
}

$appKey = Read-Required "AppKey"
$appSecret = Read-SecretPlain "AppSecret (hidden)"
$account = Read-Required "Account number first 8 digits"
if ($account -notmatch '^\d{8}$') {
    throw "Account number must be the first 8 digits only."
}

$product = Read-Host "Account product code, last 2 digits (default: 01)"
if ([string]::IsNullOrWhiteSpace($product)) { $product = "01" }
$product = $product.Trim()
if ($product -notmatch '^\d{2}$') {
    throw "Product code must be 2 digits."
}

$htsId = Read-Host "HTS ID (optional, needed for realtime account notices)"
if ($null -eq $htsId) { $htsId = "" }
$htsId = $htsId.Trim()

$configDir = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

if (Test-Path $ConfigPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.bak.$stamp" -Force
}

$myApp = ""
$mySec = ""
$paperApp = ""
$paperSec = ""
$myAcctStock = ""
$myPaperStock = ""

if ($envChoice -eq "prod") {
    $myApp = $appKey
    $mySec = $appSecret
    $myAcctStock = $account
}
else {
    $paperApp = $appKey
    $paperSec = $appSecret
    $myPaperStock = $account
}

$content = @"
my_app: $(Quote-Yaml $myApp)
my_sec: $(Quote-Yaml $mySec)

paper_app: $(Quote-Yaml $paperApp)
paper_sec: $(Quote-Yaml $paperSec)

my_htsid: $(Quote-Yaml $htsId)

my_acct_stock: $(Quote-Yaml $myAcctStock)
my_acct_future: ""
my_paper_stock: $(Quote-Yaml $myPaperStock)
my_paper_future: ""

my_prod: $(Quote-Yaml $product)

prod: "https://openapi.koreainvestment.com:9443"
ops: "ws://ops.koreainvestment.com:21000"
vps: "https://openapivts.koreainvestment.com:29443"
vops: "ws://ops.koreainvestment.com:31000"

my_token: ""

my_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
"@

Set-Content -LiteralPath $ConfigPath -Value $content -Encoding UTF8

Write-Host ""
Write-Host "Saved KIS config." -ForegroundColor Green
Write-Host "Environment: $envChoice"
Write-Host "Config path: $ConfigPath"
Write-Host "Next test command:"
if ($envChoice -eq "prod") {
    Write-Host "python -c `"import kis_auth as ka; ka.auth('prod'); print('token issued:', bool(ka.getTREnv().my_token))`""
}
else {
    Write-Host "python -c `"import kis_auth as ka; ka.auth('vps'); print('token issued:', bool(ka.getTREnv().my_token))`""
}
