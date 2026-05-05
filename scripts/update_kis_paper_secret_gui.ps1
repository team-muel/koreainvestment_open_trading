param(
    [string]$ConfigPath = "$HOME\KIS\config\kis_devlp.yaml"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Strip-ControlChars {
    param([string]$Value)
    if ($null -eq $Value) { return "" }
    return -join ($Value.ToCharArray() | Where-Object {
        $code = [int][char]$_
        $code -eq 9 -or $code -eq 10 -or $code -eq 13 -or $code -ge 32
    })
}

function Get-YamlValue {
    param([string[]]$Lines, [string]$Key)
    foreach ($line in $Lines) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*:\s*(.*)\s*$") {
            $value = $Matches[1].Trim()
            if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
                return $value.Substring(1, $value.Length - 2).Replace('\"', '"').Replace('\\', '\')
            }
            return $value
        }
    }
    return ""
}

function Quote-Yaml {
    param([string]$Value)
    if ($null -eq $Value) { $Value = "" }
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

if (-not (Test-Path $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
$clean = Strip-ControlChars $raw
$lines = $clean -split "`r?`n"

$form = New-Object System.Windows.Forms.Form
$form.Text = "KIS Paper AppSecret"
$form.Size = New-Object System.Drawing.Size(520, 170)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Text = "Paste Paper AppSecret. It will be saved only to local kis_devlp.yaml."
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(16, 16)
$form.Controls.Add($label)

$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Location = New-Object System.Drawing.Point(16, 48)
$textBox.Size = New-Object System.Drawing.Size(470, 24)
$textBox.UseSystemPasswordChar = $true
$form.Controls.Add($textBox)

$okButton = New-Object System.Windows.Forms.Button
$okButton.Text = "Save"
$okButton.Location = New-Object System.Drawing.Point(330, 88)
$okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.AcceptButton = $okButton
$form.Controls.Add($okButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "Cancel"
$cancelButton.Location = New-Object System.Drawing.Point(410, 88)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.CancelButton = $cancelButton
$form.Controls.Add($cancelButton)

$result = $form.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    throw "Cancelled"
}

$secret = Strip-ControlChars $textBox.Text.Trim()
if ([string]::IsNullOrWhiteSpace($secret)) {
    throw "AppSecret cannot be empty."
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.bak.$stamp" -Force

$content = @"
my_app: $(Quote-Yaml (Get-YamlValue $lines "my_app"))
my_sec: $(Quote-Yaml (Get-YamlValue $lines "my_sec"))

paper_app: $(Quote-Yaml (Get-YamlValue $lines "paper_app"))
paper_sec: $(Quote-Yaml $secret)

my_htsid: $(Quote-Yaml (Get-YamlValue $lines "my_htsid"))

my_acct_stock: $(Quote-Yaml (Get-YamlValue $lines "my_acct_stock"))
my_acct_future: $(Quote-Yaml (Get-YamlValue $lines "my_acct_future"))
my_paper_stock: $(Quote-Yaml (Get-YamlValue $lines "my_paper_stock"))
my_paper_future: $(Quote-Yaml (Get-YamlValue $lines "my_paper_future"))

my_prod: $(Quote-Yaml (Get-YamlValue $lines "my_prod"))

prod: "https://openapi.koreainvestment.com:9443"
ops: "ws://ops.koreainvestment.com:21000"
vps: "https://openapivts.koreainvestment.com:29443"
vops: "ws://ops.koreainvestment.com:31000"

my_token: ""

my_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
"@

Set-Content -LiteralPath $ConfigPath -Value $content -Encoding UTF8
Write-Host "Updated paper_sec in $ConfigPath"
