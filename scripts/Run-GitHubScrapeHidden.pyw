import subprocess
import sys


POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
TRIGGER_SCRIPT = r"C:\Users\taker\Documents\Codex\pinefield\scripts\Trigger-GitHubScrape.ps1"


command = [
    POWERSHELL,
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    TRIGGER_SCRIPT,
    *sys.argv[1:],
]

completed = subprocess.run(
    command,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    # Detached PowerShell can skip its command yet report success; hide its console instead.
    creationflags=subprocess.CREATE_NO_WINDOW,
)
raise SystemExit(completed.returncode)
