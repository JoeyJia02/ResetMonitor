"""Interactive visual demo. No vault access, network, or mail delivery."""
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import settings_gui as gui
from monitor.settings_controller import SettingsController


def main():
    gui.SENDER = "demo@example.com"
    gui.RECIPIENT = "you@example.com"
    present = [True]
    def save(_):
        present[0] = True
    def clear():
        old, present[0] = present[0], False
        return old
    def runner(events, _):
        events.put(("done", "演示已完成，没有访问外部服务或发送邮件。", True))
    root = tk.Tk()
    gui.SettingsWindow(root,
        controller=SettingsController(saver=save, exists=lambda: present[0], deleter=clear),
        plan_loader=lambda: {"manual": True, "ready": True, "sender": gui.SENDER,
            "recipient": gui.RECIPIENT, "credential_channel": "演示模式，不访问凭据"},
        test_runner=runner, confirm_test=lambda *_: True,
        status_loader=lambda: {"running": False, "title": "界面预览", "detail": "演示模式 · 不连接服务",
                               "last_run": "使用虚构地址", "next_run": "13:00"})
    root.title("ResetMonitor · 界面预览（演示数据）")
    root.mainloop()


if __name__ == "__main__":
    main()
