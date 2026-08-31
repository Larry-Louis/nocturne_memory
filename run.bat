# 1. 找到并杀掉旧进程（在 PowerShell 里）
Get-Process python | Where-Object { $_.Path -match 'nocturne_memory' } | Stop-Process -Force

# 2. 重新启动
venv\Scripts\python.exe backend\run_sse.py