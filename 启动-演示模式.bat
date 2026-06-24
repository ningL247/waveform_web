@echo off
cd /d "%~dp0"
set PORT=8090

echo ========================================
echo   波形上位机 - 演示模式 (回放CSV)
echo   网页地址:  http://localhost:%PORT%
echo   关闭: 直接关掉本窗口, 或按 Ctrl+C
echo ========================================
echo.

REM ---- 清理占用端口的旧进程(上次没关干净也能直接启动)----
echo [启动前] 检查端口 %PORT% 是否被占用...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ('  结束占用端口的旧进程 PID=' + $_.OwningProcess); Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo.

REM ---- 后台等服务器真正起来再开浏览器(避免localhost拒绝连接)----
start "" powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',%PORT%);Start-Process 'http://localhost:%PORT%';break}catch{Start-Sleep 1}}"

python server.py --demo "D:\DesktopD\data\data\1zhongjiawen.csv" --rate 500 --loop --port %PORT%
pause