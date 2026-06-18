# Step 50: 配信用 EIP の確保 + Route 53 A レコード。

. $PSScriptRoot/common.ps1

Write-Step "Elastic IP (配信 EC2 用) を確保"
if (-not (Get-State eip_alloc_id)) {
    $alloc = aws ec2 allocate-address --domain vpc `
        --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=bs-streamer-eip},{Key=Project,Value=block-stacker}]" `
        --query "{Id:AllocationId,Ip:PublicIp}" | ConvertFrom-Json
    Set-State eip_alloc_id $alloc.Id
    Set-State eip_public_ip $alloc.Ip
    Write-Done "alloc=$($alloc.Id) ip=$($alloc.Ip)"
} else {
    Write-Done "既存 eip = $(Get-State eip_alloc_id) ($(Get-State eip_public_ip))"
}

$eip = Get-State eip_public_ip
$zoneName = $script:BS.DomainZone
$recName  = $script:BS.DomainName

Write-Step "Route 53 ホストゾーン ($zoneName) を解決"
$zoneId = aws route53 list-hosted-zones-by-name --dns-name $zoneName `
    --query "HostedZones[?Name=='$zoneName.'] | [0].Id" --output text
if (-not $zoneId -or $zoneId -eq "None") {
    throw "Hosted zone $zoneName が見つかりません。先に Route 53 で作成し、レジストラ側で NS を委任してください。"
}
$zoneId = $zoneId -replace "/hostedzone/", ""
Set-State route53_zone_id $zoneId
Write-Done "zone id = $zoneId"

Write-Step "A レコード $recName → $eip を UPSERT"
$change = @"
{
  "Changes":[{
    "Action":"UPSERT",
    "ResourceRecordSet":{
      "Name":"$recName",
      "Type":"A",
      "TTL":300,
      "ResourceRecords":[{"Value":"$eip"}]
    }
  }]
}
"@
$tmp = New-TemporaryFile
Set-Content $tmp -Value $change -Encoding utf8
aws route53 change-resource-record-sets --hosted-zone-id $zoneId --change-batch "file://$($tmp.FullName)" | Out-Null
Remove-Item $tmp

Write-Host ""
Write-Host "[bs] 50_eip 完了 ($recName → $eip)" -ForegroundColor Green
