param([ValidateSet('status','logs','health','tunnel','vnc-tunnel','stop','start','pause')][string]$Action='status')
$ErrorActionPreference='Stop'
# Server address and key stay on this computer, not in git (the repository may be public):
#   setx TRADINGBOT_SERVER "ec2-user@<server-ip>"
#   setx TRADINGBOT_SSH_KEY "C:\Users\<you>\Downloads\bot.pem"   (optional; this is the default)
$Server=$env:TRADINGBOT_SERVER
if (-not $Server) { throw 'Set TRADINGBOT_SERVER first, e.g.: setx TRADINGBOT_SERVER "ec2-user@<server-ip>" (then open a new terminal)' }
$Key=if ($env:TRADINGBOT_SSH_KEY) { $env:TRADINGBOT_SSH_KEY } else { Join-Path $env:USERPROFILE 'Downloads\bot.pem' }
if (!(Test-Path $Key)) { throw "SSH key not found: $Key" }
switch ($Action) {
  'status' { ssh -i $Key $Server 'cd /opt/tradingbot && sudo docker compose -f deploy/linux/compose.yaml ps' }
  'logs' { ssh -i $Key $Server 'sudo docker compose -f /opt/tradingbot/deploy/linux/compose.yaml logs --tail=100' }
  'health' { ssh -i $Key $Server 'cd /opt/tradingbot && sudo docker compose -f deploy/linux/compose.yaml exec -T mt5bot wine "C:\Program Files\Python312\python.exe" scripts/health_check.py' }
  'tunnel' { ssh -i $Key -N -L 8501:127.0.0.1:8501 $Server }
  'vnc-tunnel' { ssh -i $Key -N -L 5900:127.0.0.1:5900 $Server }
  'stop' { ssh -i $Key $Server 'cd /opt/tradingbot && ./stop_bot.sh' }
  'pause' { ssh -i $Key $Server 'cd /opt/tradingbot && ./pause_bot.sh' }
  'start' { ssh -i $Key $Server 'cd /opt/tradingbot && ./start_bot.sh force' }
}
