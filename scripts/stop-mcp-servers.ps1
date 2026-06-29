# Stop all awlab-* MCP server processes
Get-Process | Where-Object { $_.ProcessName -like "awlab-*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "All awlab-* MCP server processes stopped."
