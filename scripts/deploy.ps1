$ErrorActionPreference='Stop'
# Same settings as scripts/server.ps1 (kept out of git):
$Server=$env:TRADINGBOT_SERVER
if (-not $Server) { throw 'Set TRADINGBOT_SERVER first, e.g.: setx TRADINGBOT_SERVER "ec2-user@<server-ip>" (then open a new terminal)' }
$Key=if ($env:TRADINGBOT_SSH_KEY) { $env:TRADINGBOT_SSH_KEY } else { Join-Path $env:USERPROFILE 'Downloads\bot.pem' }
if (!(Test-Path $Key)) { throw "SSH key not found: $Key" }
$python=Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (!(Test-Path $python)) { $python=(Get-Command python -ErrorAction Stop).Source }
& $python -m pytest
if ($LASTEXITCODE -ne 0) { throw 'Local tests failed; deployment stopped.' }
$archive=Join-Path $env:TEMP 'tradingbot-deploy.tar'
git archive --format=tar HEAD -o $archive
if ($LASTEXITCODE -ne 0) { throw 'Could not create source archive.' }
scp -i $Key $archive "${Server}:/home/ec2-user/tradingbot-deploy.tar"
ssh -i $Key $Server 'set -e; sudo tar -xf /home/ec2-user/tradingbot-deploy.tar -C /opt/tradingbot; sudo chown -R ec2-user:ec2-user /opt/tradingbot; chmod 600 /opt/tradingbot/.env; cd /opt/tradingbot; sudo docker compose -f deploy/linux/compose.yaml build; sudo docker compose -f deploy/linux/compose.yaml up -d; sudo docker compose -f deploy/linux/compose.yaml ps'
if ($LASTEXITCODE -ne 0) { throw 'Remote deploy or health command failed.' }
