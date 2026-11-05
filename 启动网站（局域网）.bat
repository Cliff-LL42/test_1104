@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  =========================================================
echo    TRPG 地图工坊 - 局域网模式
echo    启动后浏览器会自动打开，同一 WiFi 的朋友也能访问
echo    需要跨网络共享？请用「启动并分享到公网.bat」
echo  =========================================================
echo.
python server.py --open
echo.
echo 服务已结束。按任意键关闭窗口。
pause >nul
