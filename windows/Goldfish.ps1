param(
 [ValidateSet('Check','Install','Update','Configure','Start','StartCompaction','StartExplicit','StartReadOnly','Stop','Diagnose','Chat','Uninstall')][string]$Action='Check',
 [string]$DataDir=$env:GOLDFISH_DATA_DIR,[string]$DshExecutable=$env:GOLDFISH_DSH,
 [string]$DshHome=$env:DSH_HOME,[string]$Profile='web',[string]$PythonExecutable='',
 [ValidateRange(1024,65535)][int]$Port=18320
)
$ErrorActionPreference='Stop'
$Root=Split-Path $PSScriptRoot -Parent
if(!$DataDir){$DataDir=Join-Path $env:LOCALAPPDATA 'GoldfishMemory'}
$DataDir=[IO.Path]::GetFullPath($DataDir)
$env:PYTHONPATH=$Root;$env:PYTHONUTF8='1'
$Python=Join-Path $DataDir 'venv\Scripts\python.exe'
$Settings=Join-Path $DataDir 'installation.json'
if(Test-Path -LiteralPath $Settings){
 $saved=Get-Content -LiteralPath $Settings -Raw -Encoding UTF8 | ConvertFrom-Json
 if(!$DshExecutable){$DshExecutable=$saved.DshExecutable};if(!$DshHome){$DshHome=$saved.DshHome}
 if(!$PSBoundParameters.ContainsKey('Profile')){$Profile=$saved.Profile}
 if(!$PSBoundParameters.ContainsKey('Port') -and $saved.Port){$Port=$saved.Port}
}
if(!$DshExecutable){$command=Get-Command dsh.cmd -ErrorAction SilentlyContinue;if($command){$DshExecutable=$command.Source}}
if(!$DshHome){$DshHome=Join-Path $env:USERPROFILE '.dsh'}
$DshHome=[IO.Path]::GetFullPath($DshHome)
if($Profile -notmatch '^[A-Za-z0-9_-]+$'){throw 'Invalid profile name.'}
$env:DSH_HOME=$DshHome
function Run($Exe,$Arguments){& $Exe @Arguments;if($LASTEXITCODE -ne 0){throw "Command failed: $Exe (exit $LASTEXITCODE)"}}
function RequireDsh {
 if(!$DshExecutable -or !(Test-Path -LiteralPath $DshExecutable)){throw 'Pass -DshExecutable <absolute dsh.cmd path> and -DshHome <DSH data directory>. See README.'}
 $env:PATH=(Split-Path $DshExecutable -Parent)+';'+$env:PATH
}
function PythonBase {if($PythonExecutable){return @($PythonExecutable)};if(Get-Command py -ErrorAction SilentlyContinue){return @('py','-3.11')};return @('python')}
function CheckRuntime {
 $base=@(PythonBase);$more=@();if($base.Length -gt 1){$more=@($base[1..($base.Length-1)])}
 Run $base[0] ($more+@('-c','import sys; assert sys.version_info >= (3,10) and sys.maxsize > 2**32; print(sys.version)'))
 Run 'node' @('-e','const v=process.versions.node.split(String.fromCharCode(46)).map(Number);if(v[0]<24||(v[0]===24&&v[1]<9))process.exit(1);console.log(process.version)')
}
function SaveSettings {
 New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
 @{DshExecutable=$DshExecutable;DshHome=$DshHome;Profile=$Profile;Port=$Port} | ConvertTo-Json | Set-Content -LiteralPath $Settings -Encoding UTF8
}
function StopService {
 $file=Join-Path $DataDir 'processes.json';if(!(Test-Path -LiteralPath $file)){return}
 foreach($entry in (Get-Content -LiteralPath $file -Raw | ConvertFrom-Json)){
  $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.Id)" -ErrorAction SilentlyContinue
  if($process -and $process.CreationDate.ToUniversalTime() -eq ([datetime]$entry.Created).ToUniversalTime() -and $process.CommandLine -match 'goldfish\.server'){
   foreach($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($entry.Id)" -ErrorAction SilentlyContinue)){
    if($child.CommandLine -match 'goldfish\.server'){Stop-Process -Id $child.ProcessId -ErrorAction Stop}
   }
   Stop-Process -Id $entry.Id -ErrorAction SilentlyContinue
  }
 }
 Remove-Item -LiteralPath $file
}
function StartService($Mode){
 if(!(Test-Path -LiteralPath $Python)){throw 'Run Install first.'}
 if(Test-Path -LiteralPath (Join-Path $DataDir 'processes.json')){throw 'Run Stop before Start.'}
 if(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue){throw 'Selected port is occupied. No process was stopped.'}
 $database=Join-Path $DataDir 'memory.sqlite3';if(!(Test-Path -LiteralPath $database)){throw 'Run Install first; database missing.'}
 $arguments=@('-m','goldfish.server','--db',$database,'--port',[string]$Port,'--read-only')
 if($Mode -ne 'StartReadOnly'){$arguments+=@('--allow-remember','--remember-key-file',(Join-Path $DataDir 'remember-write.key'))}
 if($Mode -eq 'Start' -or $Mode -eq 'StartCompaction'){$arguments+='--allow-compaction'}
 $quoted=($arguments | ForEach-Object {'"'+$_+'"'}) -join ' '
 $process=Start-Process -FilePath $Python -ArgumentList $quoted -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $DataDir 'memory.stdout.log') -RedirectStandardError (Join-Path $DataDir 'memory.stderr.log')
 $info=Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)";if(!$info){throw 'Backend exited. Inspect local memory.stderr.log; do not publish it.'}
 ConvertTo-Json -InputObject @(@{Id=$process.Id;Created=$info.CreationDate.ToUniversalTime().ToString('o')}) | Set-Content -LiteralPath (Join-Path $DataDir 'processes.json') -Encoding UTF8
 for($index=0;$index -lt 30;$index++){
  try{$health=Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2;if($health.version -eq '0.8.0' -and $health.query_only){Write-Host "Goldfish 0.8.0 ready on localhost:$Port. Main model stays in DSH.";return}}catch{}
  Start-Sleep -Seconds 1
 }
 throw 'Readiness failed. Run Diagnose; process record is retained for Stop.'
}
try {switch($Action){
 'Check' {CheckRuntime;RequireDsh;Write-Host 'Tested DSH contract: 0.2.1-rc.5.'}
 {$_ -in 'Install','Update'} {
  CheckRuntime;RequireDsh
  if(Test-Path -LiteralPath (Join-Path $DataDir 'processes.json')){throw 'Run Stop before installing/updating.'}
  SaveSettings
  if(!(Test-Path -LiteralPath $Python)){
   $base=@(PythonBase);$more=@();if($base.Length -gt 1){$more=@($base[1..($base.Length-1)])}
   Run $base[0] ($more+@('-m','venv',(Join-Path $DataDir 'venv')))
  }
  Run $Python @('-m','pip','install','--no-index','-r',(Join-Path $PSScriptRoot 'requirements.txt'))
  $env:npm_config_ignore_workspace_root_check='true'
  Run $DshExecutable @('plugin','--profile',$Profile,'add',('link:'+(Join-Path $Root 'dsh-plugin')))
  Run $Python @((Join-Path $Root 'scripts\migrate_database.py'),'--db',(Join-Path $DataDir 'memory.sqlite3'))
  Write-Host 'Installed without replacing existing memories. Keep this source directory in place. Next: Start, then Chat.'
 }
 'Configure' {RequireDsh;SaveSettings;Write-Host 'Saved DSH paths/profile only. Set main model and credentials in DSH; Goldfish asks for no model key.'}
 {$_ -in 'Start','StartCompaction','StartExplicit','StartReadOnly'} {StartService $Action}
 'Stop' {StopService;Write-Host 'Recorded Goldfish backend stopped; DSH is separate.'}
 'Chat' {RequireDsh;Run $DshExecutable @('web','--profile',$Profile)}
 'Diagnose' {CheckRuntime;try{Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3 | ConvertTo-Json}catch{Write-Host 'Backend unavailable. Inspect private local logs; do not publish them.'}}
 'Uninstall' {StopService;RequireDsh;Run $DshExecutable @('plugin','--profile',$Profile,'remove','dsh-goldfish-memory-local');Write-Host 'Plugin detached. Database, keys, sessions, backups, venv and source retained. See README.'}
}}catch{Write-Host ('FAILED: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
