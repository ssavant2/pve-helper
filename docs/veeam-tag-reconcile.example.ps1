<#
.SYNOPSIS
Reads desired backup groups from pve-helper and hands each group to a local adapter.

.DESCRIPTION
The pve-helper API is stable, while the commands used to edit a Veeam Proxmox job
depend on the Veeam release and installed plug-in. This example deliberately does
not invent Veeam cmdlet names. Supply ApplyGroup after confirming the commands
available on the Veeam server with Get-VBRCommand.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $BaseUrl,

    [string] $Token = $env:PVEHELPER_INTEGRATION_TOKEN,

    [string] $PolicyTag,

    [scriptblock] $ApplyGroup,

    [switch] $FailOnUnassigned
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Token)) {
    throw 'Set PVEHELPER_INTEGRATION_TOKEN or pass -Token.'
}

$headers = @{ Authorization = "Bearer $Token" }
$uri = "$($BaseUrl.TrimEnd('/'))/api/v1/backup-groups.json"
$inventory = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers

if ($inventory.conflicts.Count -gt 0) {
    $details = $inventory.conflicts | ForEach-Object {
        "VMID $($_.vmid): $($_.policy_tags -join ', ')"
    }
    throw "Objects have more than one backup policy tag:`n$($details -join "`n")"
}

if ($inventory.unassigned.Count -gt 0) {
    $vmids = ($inventory.unassigned | ForEach-Object { $_.vmid }) -join ', '
    $message = "Objects without a backup policy tag: $vmids"
    if ($FailOnUnassigned) { throw $message }
    Write-Warning $message
}

$groups = $inventory.groups.PSObject.Properties
if ($PolicyTag) {
    $groups = $groups | Where-Object Name -eq $PolicyTag
    if (-not $groups) { throw "Backup policy tag '$PolicyTag' was not returned by pve-helper." }
}

foreach ($group in $groups) {
    $tag = $group.Name
    $guests = @($group.Value)
    Write-Host "$tag -> $($guests.Count) object(s): $(($guests.vmid) -join ', ')"
    if ($ApplyGroup) {
        & $ApplyGroup $tag $guests
    }
}

if (-not $ApplyGroup) {
    Write-Warning 'Preview only. Pass -ApplyGroup with the job-update commands supported by the installed Veeam version.'
}
