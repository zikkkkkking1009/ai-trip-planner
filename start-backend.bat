@echo off
rem ai-trip-planner backend resident launcher (Oray tunnel maps to 127.0.0.1:8000)
rem NOTE 2026-10-10: the scheduled task "ai-trip-backend" no longer exists on this
rem machine (verified with Get-ScheduledTask), so this does NOT autostart at logon
rem any more -- start it manually, or re-register the task (see 怎么启动.md sec 8).
rem Skips when port 8000 is already listening (avoid stomping a live instance).
rem NOTE: keep this file ASCII-only - cmd.exe parses .bat in ANSI/GBK, UTF-8
rem Chinese comments get mangled into garbage commands (bitten 2026-10-04).

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [%date% %time%] port 8000 already listening, skip >> "D:\workby room\ai-trip-planner\data\backend_start.log"
    exit /b 0
)

echo [%date% %time%] starting backend >> "D:\workby room\ai-trip-planner\data\backend_start.log"
cd /d "D:\workby room\ai-trip-planner\backend"
rem --no-proxy-headers (2026-10-04): uvicorn trusts XFF from 127.0.0.1 by default and
rem rewrites request.client; the Oray tunnel is pass-through so forged headers arrive
rem intact = rate-limit bypass (measured, see COLLAB_LOG). Moving to a trusted reverse
rem proxy later: remove this flag AND set TRUST_PROXY_HEADERS=1 in backend/.env.
"D:\workby room\ai-trip-planner\.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-proxy-headers >> "D:\workby room\ai-trip-planner\data\backend_start.log" 2>&1
