from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

PROJECT_DIR = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8000/staff/login"

def server_running():
    try:
        with urllib.request.urlopen(URL, timeout=2):
            return True
    except Exception:
        return False

if not server_running():
    subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "run.py")],
        cwd=str(PROJECT_DIR),
    )

    for _ in range(30):
        if server_running():
            break
        time.sleep(0.5)

chrome_paths = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
]

chrome = next((p for p in chrome_paths if p.exists()), None)

if chrome and server_running():
    subprocess.Popen([str(chrome), URL])
elif server_running():
    webbrowser.open(URL)
else:
    print("เปิดเว็บเซิร์ฟเวอร์ไม่สำเร็จ กรุณาตรวจสอบ run.py")