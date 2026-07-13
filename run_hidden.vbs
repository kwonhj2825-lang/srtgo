' server.py 를 창 없이(숨김) 백그라운드로 실행하는 런처
Set WShell = CreateObject("WScript.Shell")
WShell.Run """C:\Users\SDS\Downloads\srtgo\start_server.bat""", 0, False
