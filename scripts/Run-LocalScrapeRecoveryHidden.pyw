"""Launch the daily missing-source recovery without opening a console window."""
from pathlib import Path
import subprocess
import sys


python = Path(sys.executable).with_name('python.exe')
controller = Path(__file__).with_name('recover_missing_daily_sources.py')
result = subprocess.run(
    [str(python), '-B', '-X', 'utf8', str(controller), *sys.argv[1:]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
raise SystemExit(result.returncode)
