"""Double-click desktop entry; pythonw does not open a console window."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from monitor.settings_gui import main
    main()
except Exception:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, "无法打开设置窗口，请确认项目文件和 Python 安装完整。",
                                    "ResetMonitor", 0x10)
