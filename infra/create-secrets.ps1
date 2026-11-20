# Prompts for each app secret (input hidden) and stores it in Secret Manager.
# Press Enter on an empty prompt to skip a secret. Re-running adds a new version.
#   powershell -ExecutionPolicy Bypass -File infra\create-secrets.ps1
$ErrorActionPreference = "Stop"
$gcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
if (-not (Test-Path $gcloud)) { $gcloud = "gcloud" }
$project = "corvenia-search-monitor"

$secrets = [ordered]@{
    "BING_API_KEY"      = "Bing Webmaster Tools > Settings (gear) > API access > API key"
    "CLARITY_API_TOKEN" = "clarity.microsoft.com > your project > Settings > Data Export > Generate new API token"
    "WEBFLOW_API_TOKEN" = "Webflow > Corvenia site settings > Apps & integrations > API access > Generate API token (read: sites, pages, CMS, forms)"
}

foreach ($name in $secrets.Keys) {
    Write-Host ""
    Write-Host "$name" -ForegroundColor Green
    Write-Host "  Where: $($secrets[$name])"
    $secure = Read-Host "  Paste value (hidden, Enter to skip)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ([string]::IsNullOrWhiteSpace($plain)) { Write-Host "  skipped"; continue }

    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp.FullName, $plain.Trim())
        & $gcloud secrets describe $name --project $project *> $null
        if ($LASTEXITCODE -eq 0) {
            & $gcloud secrets versions add $name --project $project --data-file $tmp.FullName *> $null
            Write-Host "  new version added" -ForegroundColor Green
        } else {
            & $gcloud secrets create $name --project $project --replication-policy automatic --data-file $tmp.FullName *> $null
            Write-Host "  created" -ForegroundColor Green
        }
    } finally {
        Remove-Item $tmp.FullName -Force
        $plain = $null
    }
}
Write-Host ""
Write-Host "Done. Secrets now in Secret Manager:"
& $gcloud secrets list --project $project --format "value(name)"
