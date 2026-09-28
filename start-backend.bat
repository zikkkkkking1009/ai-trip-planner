@echo off
rem ai-trip-planner 后端常驻启动（花生壳免费档：隧道连本机 127.0.0.1:8000）
rem 由计划任务 "ai-trip-backend" 在登录时调用；也可手动双击运行。
rem 已有实例在监听 8000 就不重复启动（避免端口冲突把旧实例顶掉）。

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [%date% %time%] port 8000 already listening, skip >> "D:\workby room\ai-trip-planner\data\backend_start.log"
    exit /b 0
)

echo [%date% %time%] starting backend >> "D:\workby room\ai-trip-planner\data\backend_start.log"
cd /d "D:\workby room\ai-trip-planner\backend"
"D:\workby room\ai-trip-planner\.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 >> "D:\workby room\ai-trip-planner\data\backend_start.log" 2>&1
