param([ValidateSet('Check','Install','Configure','Start','Stop','Diagnose','Chat','Uninstall')][string]$Action='Check')
$ErrorActionPreference='Stop'
$Root=Split-Path $PSScriptRoot -Parent
$Data=Join-Path $env:LOCALAPPDATA 'GoldfishMemory'
$Python=Join-Path $Data 'venv\Scripts\python.exe'
$Dsh=Join-Path $Data 'tools\node_modules\.bin\dsh.cmd'
$env:DSH_HOME=Join-Path $Data 'dsh'
$env:PYTHONPATH=$Root
function Run($Exe,$Arguments) { & $Exe @Arguments; if($LASTEXITCODE -ne 0){throw "Command failed: $Exe (exit $LASTEXITCODE)"} }
function LoadConfig {
  $file=Join-Path $Data 'config.json'
  if(!(Test-Path $file)){throw 'Run Install first.'}
  $c=Get-Content $file -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach($v in $c.PSObject.Properties){if($v.Name -notmatch '^GOLDFISH_'){throw 'Invalid config name'};[Environment]::SetEnvironmentVariable($v.Name,[string]$v.Value,'Process')}
}
function LoadKey {
  if($env:GOLDFISH_LLM_PROVIDER -eq 'ollama' -or $env:GOLDFISH_LLM_API_KEY){return}
  $file=Join-Path $Data 'deepseek-key.xml'
  if(!(Test-Path $file)){throw 'Run Configure to save your API key for this Windows account.'}
  $cred=Import-Clixml $file
  $env:GOLDFISH_LLM_API_KEY=$cred.GetNetworkCredential().Password
}
function StopServices {
  $file=Join-Path $Data 'processes.json'
  if(Test-Path $file){
    foreach($entry in (Get-Content $file -Raw | ConvertFrom-Json)){
      $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.Id)" -ErrorAction SilentlyContinue
      # Never kill a reused PID or an unrelated local service.
      if($p -and $p.CreationDate.ToUniversalTime().ToString('o') -eq $entry.Created -and $p.CommandLine -like '*goldfish*'){
        Stop-Process -Id $entry.Id -ErrorAction Stop
      }
    }
    Remove-Item $file
  }
}
function SpawnService($Module,$Arguments,$Name) {
  $out=Join-Path $Data "$Name.stdout.log"; $err=Join-Path $Data "$Name.stderr.log"
  $args2=@('-m',$Module)+$Arguments
  # Paths may include spaces. All arguments here are controlled local paths.
  $quoted=($args2 | ForEach-Object {'"'+$_+'"'}) -join ' '
  $p=Start-Process $Python -ArgumentList $quoted -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
  $info=Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)"
  if(!$info){throw "Service exited: see $err"}
  return @{Id=$p.Id;Created=$info.CreationDate.ToUniversalTime().ToString('o')}
}
function WaitUrl($Url) {
  for($i=0;$i -lt 60;$i++) {try {return Invoke-RestMethod $Url -TimeoutSec 2} catch {Start-Sleep -Seconds 1}}
  throw "Service did not start: $Url. Run Diagnose."
}
try {
 switch($Action){
 'Check' {
   Run 'py' @('-3','-c','import sys; assert sys.version_info >= (3,10) and sys.maxsize > 2**32; print(sys.version)')
   Run 'node' @('-e','const v=process.versions.node.split(".").map(Number);if(v[0]<24||(v[0]===24&&v[1]<9))process.exit(1);console.log(process.version)')
   Run 'npm.cmd' @('--version')
   Write-Host 'Use 64-bit Python 3.11/3.12 and Node 24.9 or later (Node 24 LTS recommended).'
 }
 'Install' {
   & $PSCommandPath Check
   New-Item $Data -ItemType Directory -Force | Out-Null
   if(!(Test-Path $Python)){Run 'py' @('-3','-m','venv',(Join-Path $Data 'venv'))}
   Run $Python @('-m','pip','install','-r',(Join-Path $PSScriptRoot 'requirements.txt'))
   Run $Python @((Join-Path $Root 'scripts\download_embedding.py'),'--output',(Join-Path $Data 'models\bge-small-zh'))
   if(!(Test-Path (Join-Path $Data 'config.json'))){Copy-Item (Join-Path $PSScriptRoot 'config.example.json') (Join-Path $Data 'config.json')}
   Run 'npm.cmd' @('install','--prefix',(Join-Path $Data 'tools'),'@deepseek-ai/dsh@0.2.1-rc.5','--no-audit','--no-fund')
   if(!(Test-Path (Join-Path $env:DSH_HOME 'profiles\goldfish-test'))){Run $Dsh @('--profile','goldfish-test','--from-default-profile','web','--dump-config')}
   Run $Dsh @('plugin','--profile','goldfish-test','add',('link:'+(Join-Path $Root 'dsh-plugin')))
   Run $Python @((Join-Path $Root 'scripts\migrate_database.py'),'--db',(Join-Path $Data 'memory.sqlite3'))
   Write-Host 'Installed in your user folder. Next: Configure, Start, Chat. Do not move the project folder after linking the plugin.'
 }
 'Configure' {
   if(!(Test-Path $Data)){throw 'Run Install first.'}
   $secret=Read-Host 'DeepSeek API key (hidden input)' -AsSecureString
   $cred=New-Object -TypeName System.Management.Automation.PSCredential -ArgumentList 'deepseek',$secret
   $cred | Export-Clixml (Join-Path $Data 'deepseek-key.xml')
   Write-Host 'Saved with Windows account encryption. DSH chat model is configured separately in its UI.'
 }
 'Start' {
   LoadConfig; LoadKey
   if(Test-Path (Join-Path $Data 'processes.json')){throw 'Existing process record. Run Stop before Start.'}
   foreach($port in @(18320,18321)){if(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue){throw "Port $port already occupied; no process was stopped."}}
   Run $Python @((Join-Path $Root 'scripts\migrate_database.py'),'--db',(Join-Path $Data 'memory.sqlite3'))
   $records=@()
   try {
     if($env:GOLDFISH_EMBED_URL -eq 'http://127.0.0.1:18321'){
       $records+=SpawnService 'evals.local_embedding_server' @('--model-dir',(Join-Path $Data 'models\bge-small-zh'),'--port','18321') 'embedding'
       $null=WaitUrl 'http://127.0.0.1:18321'
     }
     $records+=SpawnService 'goldfish.server' @('--db',(Join-Path $Data 'memory.sqlite3'),'--port','18320') 'memory'
     $null=WaitUrl 'http://127.0.0.1:18320/health'
     Write-Host 'Goldfish ready on localhost:18320. Next: Chat.'
   } finally {if($records.Count){ConvertTo-Json -InputObject @($records) | Set-Content (Join-Path $Data 'processes.json') -Encoding UTF8}}
 }
 'Stop' {StopServices; Write-Host 'Recorded Goldfish services stopped; close DSH separately.'}
 'Diagnose' {
   Write-Host "Project: $Root`nUser data: $Data"
   & $PSCommandPath Check
   foreach($url in @('http://127.0.0.1:18320/health','http://127.0.0.1:18320/providers','http://127.0.0.1:18320/stats','http://127.0.0.1:18321')){
     try {Invoke-RestMethod $url -TimeoutSec 3 | ConvertTo-Json -Depth 5} catch {Write-Host "Unavailable: $url"}
   }
   Write-Host 'Inspect local *.stderr.log files if needed. Do not share keys, database, outbox, DSH history or full private logs.'
 }
 'Chat' {LoadConfig; LoadKey; $env:DEEPSEEK_API_KEY=$env:GOLDFISH_LLM_API_KEY; Run $Dsh @('--profile','goldfish-test')}
 'Uninstall' {
   StopServices
   if(Test-Path (Join-Path $Data 'deepseek-key.xml')){Remove-Item (Join-Path $Data 'deepseek-key.xml')}
   foreach($dir in @('venv','tools')){if(Test-Path (Join-Path $Data $dir)){Remove-Item (Join-Path $Data $dir) -Recurse -Force}}
   Write-Host 'Runtime and saved Goldfish key removed. Models, config, DB, backups, logs and DSH chats are retained in your user data folder. No cloud files were removed.'
 }
 }
} catch {Write-Host ('FAILED: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
