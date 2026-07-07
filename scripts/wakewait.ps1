param(
  [ValidateSet("sleep", "wait-file", "wait-contains", "wait-command")]
  [string]$Action = "sleep",
  [string]$Duration = "",
  [int]$Seconds = 0,
  [string]$Path = "",
  [string]$Text = "",
  [string]$Command = "",
  [string]$Every = "30s",
  [string]$Timeout = "1h"
)

$ErrorActionPreference = "Stop"

function Convert-ToSeconds {
  param([string]$Value)
  if (-not $Value) { return 0 }
  $trimmed = $Value.Trim().ToLowerInvariant()
  if ($trimmed -match '^(\d+(?:\.\d+)?)(ms|s|m|h|d)?$') {
    $amount = [double]$matches[1]
    $unit = if ($matches[2]) { $matches[2] } else { "s" }
    $seconds = switch ($unit) {
      "ms" { $amount / 1000 }
      "s" { $amount }
      "m" { $amount * 60 }
      "h" { $amount * 3600 }
      "d" { $amount * 86400 }
    }
    return [Math]::Max(1, [int][Math]::Ceiling($seconds))
  }
  throw "Invalid duration: $Value. Use values like 30s, 10m, 1h, or 600."
}

function NowText {
  Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
}

function Sleep-Quiet {
  param([int]$DelaySeconds)
  if ($DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
  }
}

function Wait-Until {
  param(
    [scriptblock]$Predicate,
    [int]$EverySeconds,
    [int]$TimeoutSeconds
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (& $Predicate) { return $true }
    $remaining = [Math]::Max(0, [int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds))
    Sleep-Quiet ([Math]::Min($EverySeconds, $remaining))
  }
  return (& $Predicate)
}

if ($Action -eq "sleep") {
  $delay = if ($Seconds -gt 0) { $Seconds } else { Convert-ToSeconds $Duration }
  if ($delay -le 0) { throw "sleep requires -Duration or -Seconds." }
  Sleep-Quiet $delay
  Write-Output (NowText)
  exit 0
}

$everySeconds = Convert-ToSeconds $Every
$timeoutSeconds = Convert-ToSeconds $Timeout
if ($timeoutSeconds -le 0) { throw "wait actions require -Timeout." }

Write-Output "wakewait $Action start $(NowText) every=${everySeconds}s timeout=${timeoutSeconds}s"

$ok = switch ($Action) {
  "wait-file" {
    if (-not $Path) { throw "wait-file requires -Path." }
    Wait-Until { Test-Path -LiteralPath $Path } $everySeconds $timeoutSeconds
  }
  "wait-contains" {
    if (-not $Path -or $Text -eq "") { throw "wait-contains requires -Path and -Text." }
    Wait-Until {
      if (-not (Test-Path -LiteralPath $Path)) { return $false }
      return [bool](Select-String -LiteralPath $Path -SimpleMatch -Pattern $Text -ErrorAction SilentlyContinue | Select-Object -First 1)
    } $everySeconds $timeoutSeconds
  }
  "wait-command" {
    if (-not $Command) { throw "wait-command requires -Command." }
    Wait-Until {
      $global:LASTEXITCODE = 0
      try {
        Invoke-Expression $Command *> $null
        return ($LASTEXITCODE -eq 0 -and $?)
      } catch {
        return $false
      }
    } $everySeconds $timeoutSeconds
  }
}

if ($ok) {
  Write-Output "wakewait $Action ready $(NowText)"
  exit 0
}

Write-Output "wakewait $Action timeout $(NowText)"
exit 124
