param(
    [ValidateSet('Visa','Pico','Thorlabs')][string[]]$Vendor = @(),
    [string[]]$Component = @(),
    [string[]]$Model = @(),
    [switch]$VerifyOnly,
    [switch]$UseProxy,
    [string]$Cache = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv-dev\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/setup-dev.ps1 first.' }
if ($UseProxy) { throw 'Component downloads now use the shared direct-download service; -UseProxy is unsupported.' }
$mapping = @{ Visa='rs-visa'; Pico='pico-sdk'; Thorlabs='thorlabs-opm' }
$selected = @($Component) + @($Vendor | ForEach-Object { $mapping[$_] })
$action = if ($VerifyOnly) { 'verify' } else { 'prepare' }
$arguments = @('-m','lab_control','--cli','components',$action)
foreach ($id in $Model) { $arguments += @('--model',$id) }
if (-not $selected -and -not $Model) { $arguments += '--common' }
foreach ($id in $selected) { $arguments += @('--component',$id) }
if ($Cache) { $arguments += @('--cache',$Cache) }
& $python @arguments
exit $LASTEXITCODE
