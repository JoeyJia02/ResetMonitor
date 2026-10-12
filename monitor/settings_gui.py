"""Local desktop settings: masked input, Windows vault and one-shot test button."""

from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from uuid import uuid4

from monitor.settings_controller import SettingsController
from monitor.local_settings import SENDER, RECIPIENT, mask_address
from monitor.settings_status import read_background_status
from scripts.manual_test import manual_test_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = "#f4f6f8"
INK = "#182b3a"
MUTED = "#71808f"
BLUE = "#087f7a"
GREEN = "#087f5b"
RED = "#bd4141"
BORDER = "#e3e8ed"
TEST_BLOCK_REASONS = {
    "MODEL_BUDGET_LIMIT_EXCEEDED": "本轮模型费用上界超过剩余批准金额。",
    "MODEL_BUDGET_REQUEST_LIMIT_REACHED": "原批准的模型请求次数已用完。",
    "FULL_CHAIN_SOURCE_COST_UNVERIFIED": "Apify 完整费用上界尚未核定。",
    "FULL_CHAIN_PRICE_PLAN_EXPIRED": "价格方案已到期，需先更新。",
}


def test_blocking_reasons(plan):
    return "\n".join(TEST_BLOCK_REASONS.get(code, "累计预算校验未通过，运行已停止。")
                     for code in plan.get("reasons", [])) or "费用或累计额度尚未通过校验。"


def describe_test_plan(plan, *, reveal_addresses=False):
    sender = plan.get("sender", SENDER)
    recipient = plan.get("recipient", RECIPIENT)
    if not reveal_addresses:
        sender, recipient = mask_address(sender), mask_address(recipient)
    if plan.get("manual"):
        return "\n".join([
            "手动测试模式：不检查累计预算，不设价格方案到期。",
            "本轮最多：Apify 1 次（返回最多 3 条）→ DeepSeek 1 次 → QQ 邮件 1 封。",
            "上次样本：Apify 运行事件 USD 0.0009，模型费用上界 CNY 0.001676；新轮按实际用量计费。",
            "凭据：" + plan["credential_channel"] + "；发件：" + sender +
            "；收件：" + recipient + "。"])
    totals = plan.get("totals")
    history = ("已有累计批准：CNY " + totals["limit"] + " / " + str(totals["max_requests"]) + " 次模型请求；"
               "累计已用或预留：CNY " + totals["committed_cny"] + " / " + str(totals["requests"]) + " 次。"
               if totals else "累计预算记录无法校验，不能按零用量处理。")
    lines = [history,
             "本轮最多：Apify 1 次（返回最多 3 条）→ DeepSeek 1 次 → QQ 邮件 1 封。",
             "模型保守费用上界：CNY " + plan["model_maximum_cny"] + "。",
             "Apify 运行事件上限：USD " + plan["source_run_cap_usd"] + "；读取、存储和传输总上界未核定。",
             "凭据：" + plan["credential_channel"] + "；服务：Apify / DeepSeek / QQ SMTP。",
             "发件：" + sender + "；收件：" + recipient + "。"]
    if plan.get("approval_mode") == "per_manual_run":
        lines.append("手动确认仅新增本轮 1 次模型批准，上界 CNY " + plan["model_maximum_cny"] +
                     "（保守上界，不是预计费用）。旧测试次数不会锁住新一轮。")
    if not plan["ready"]:
        lines.append(test_blocking_reasons(plan))
    return "\n".join(lines)


def run_full_chain(events, run_id):
    """Child process reads the saved vault; command line contains no secrets."""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    script = PROJECT_ROOT / "scripts" / "manual_test.py"
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen([str(python), "-X", "utf8", "-B", str(script),
                                    "--run", "--run-id", run_id, "--confirmed"],
                                   cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", creationflags=flags)
        final = "未完成测试，请稍后查看项目结果。"
        success = False
        for raw in process.stdout:
            line = raw.strip()
            steps = {"[1/3]": "1 / 3  正在采集最新推文…",
                     "[2/3]": "2 / 3  正在分析本轮内容…",
                     "[3/3]": "3 / 3  正在发送测试邮件…"}
            for prefix, message in steps.items():
                if line.startswith(prefix):
                    events.put(("progress", message))
            if line.startswith("FULL_CHAIN_OK "):
                final, success = "测试完成，邮件服务器已接收。请检查收件箱。", True
            elif line.startswith("FULL_CHAIN_EMPTY "):
                final = "已发送测试邮件，但本轮没有可分析的目标推文。"
            elif line == "FULL_CHAIN_RUN_ALREADY_ATTEMPTED":
                final = "本轮已记录尝试，请查看本轮结果，避免重复发送。"
            elif line == "FULL_CHAIN_BUSY":
                final = "另一个测试正在运行，请等待它完成。"
            elif line == "FULL_CHAIN_SOURCE_COST_UNVERIFIED":
                final = "Apify 完整费用上界尚未核定，未启动采集或分析。"
            elif line == "FULL_CHAIN_CONFIRMATION_REQUIRED":
                final = "本轮尚未确认，未启动采集或分析。"
            elif line == "FULL_CHAIN_PRICE_PLAN_EXPIRED":
                final = "本次测试方案已到期，请让 Codex 更新后再测试。"
            elif line.startswith("MODEL_BUDGET_"):
                final = "本次模型预算校验未通过，已停止，未启动新的采集或分析。已保存的结果继续保留。"
            elif line.startswith("CREDENTIAL_STORE_") or line == "PROJECT_CREDENTIAL_INPUT_REQUIRED":
                final = "无法读取已保存的凭据，请重新填写并保存。"
            elif re.fullmatch(r"FAILED n=[1-3] email=FAILED SMTP_TLS_OR_AUTH_UNSUPPORTED", line):
                final = "分析已完成，但 QQ 邮箱安全连接失败。结果已保存，请勿重新采集或分析。"
            elif re.match(r"^(FAILED|UNKNOWN) ", line):
                final = "测试未完成，结果已保存。请告诉 Codex 本次测试失败。"
        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            success = False
        events.put(("done", final, success))
    except BaseException:
        events.put(("done", "无法启动测试，请确认项目文件和 Python 安装完整。", False))


class SettingsWindow:
    def __init__(self, root, *, controller=None, test_runner=None, plan_loader=None, confirm_test=None,
                 status_loader=None):
        self.root = root
        self.controller = SettingsController() if controller is None else controller
        self.test_runner = run_full_chain if test_runner is None else test_runner
        self.plan_loader = manual_test_plan if plan_loader is None else plan_loader
        self.confirm_test = self._confirm_test if confirm_test is None else confirm_test
        self.status_loader = read_background_status if status_loader is None else status_loader
        self.plan = {"ready": False}
        self.events = queue.Queue()
        self.busy = False
        self.stored = False
        self.root.title("ResetMonitor · 控制台")
        height = min(820, self.root.winfo_screenheight() - 90)
        width = min(1120, self.root.winfo_screenwidth() - 80)
        self.root.geometry(str(width) + "x" + str(height))
        self.root.minsize(min(860, width), min(650, height))
        self.root.configure(background=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self.callback_error
        self._styles()
        self._build()
        self.refresh()
        self.root.after(100, self.poll)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        font = ("Microsoft YaHei UI", 10)
        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background="white")
        style.configure("App.TLabel", background=BACKGROUND, foreground=INK, font=font)
        style.configure("Title.TLabel", background=BACKGROUND, foreground=INK,
                        font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED, font=font)
        style.configure("Card.TLabel", background="white", foreground=INK, font=font)
        style.configure("Note.TLabel", background="white", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background="white", foreground=INK,
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TEntry", padding=(12, 8), fieldbackground="#f8fafb", foreground=INK,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, borderwidth=1)
        style.map("TEntry", bordercolor=[("focus", BLUE)], lightcolor=[("focus", BLUE)],
                  darkcolor=[("focus", BLUE)])
        style.configure("TButton", font=font, padding=(16, 9), borderwidth=0,
                        focusthickness=0, focuscolor="none")
        style.configure("Primary.TButton", background=BLUE, foreground="white")
        style.map("Primary.TButton", background=[("disabled", "#d8e7e5"), ("active", "#066963")],
                  foreground=[("disabled", "#7e9995")])
        style.configure("Quiet.TButton", background="#edf2f4", foreground=INK)
        style.map("Quiet.TButton", background=[("active", "#e0e9ec")])
        style.configure("Link.TButton", background="white", foreground=BLUE, padding=(0, 5))
        style.map("Link.TButton", background=[("active", "white")])
        style.configure("Delete.TButton", background="white", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9), padding=(0, 5))
        style.map("Delete.TButton", foreground=[("active", RED)], background=[("active", "white")])
        style.configure("Card.TCheckbutton", background="white", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.map("Card.TCheckbutton", background=[("active", "white")])
        style.configure("Thin.Vertical.TScrollbar", background="#d4dde2", troughcolor=BACKGROUND,
                        borderwidth=0, arrowsize=10)
        icon = tk.PhotoImage(width=24, height=24)
        icon.put(BLUE, to=(0, 0, 24, 24))
        for rectangle in ((6, 5, 9, 19), (9, 5, 18, 8), (9, 11, 17, 14),
                          (15, 8, 18, 11), (13, 14, 16, 17), (16, 17, 19, 19)):
            icon.put("white", to=rectangle)
        self.icon = icon
        self.root.iconphoto(True, icon)

    def _card(self, parent, *, padding=18):
        border = tk.Frame(parent, background="white", highlightbackground=BORDER, highlightthickness=1)
        inside = ttk.Frame(border, style="Card.TFrame", padding=padding)
        inside.pack(fill="both", expand=True)
        return border, inside

    def _build(self):
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, background=BACKGROUND, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview,
                                  style="Thin.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        outer = ttk.Frame(canvas, style="App.TFrame", padding=(28, 14, 28, 16))
        window = canvas.create_window((0, 0), window=outer, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        outer.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        self.root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.canvas = canvas

        nav = ttk.Frame(outer, style="App.TFrame")
        nav.pack(fill="x")
        mark = tk.Canvas(nav, width=36, height=36, bg=BLUE, highlightthickness=0)
        mark.create_text(18, 18, text="R", fill="white", font=("Segoe UI", 20, "bold"))
        mark.pack(side="left", padx=(0, 12))
        ttk.Label(nav, text="ResetMonitor", style="App.TLabel",
                  font=("Segoe UI", 17, "bold")).pack(side="left")
        self.daemon_badge = tk.Label(nav, text="检查后台状态", bg="#e8edf0", fg=MUTED,
                                     font=("Microsoft YaHei UI", 9), padx=12, pady=6)
        self.daemon_badge.pack(side="right")
        ttk.Label(outer, text="重要公告，不必守着刷新。", style="Title.TLabel").pack(anchor="w", pady=(16, 6))
        ttk.Label(outer, text="关注 Codex 额外额度重置，让值得关注的消息来到你的邮箱。",
                  style="Subtitle.TLabel").pack(anchor="w")

        metrics = ttk.Frame(outer, style="App.TFrame")
        metrics.pack(fill="x", pady=(14, 10))
        metrics.columnconfigure(0, weight=1, uniform="metric")
        metrics.columnconfigure(1, weight=2, uniform="metric")
        metrics.columnconfigure(2, weight=1, uniform="metric")
        for column, title, value, detail in ((0, "监控来源", "@thsottiaux", "Tibo · 公开推文"),
                (1, "每日检查时间", "08:00   /   13:00   /   20:00", "北京时间 · 电脑需保持在线"),
                (2, "每轮采集上限", "03 条", "一次批量分析")):
            border, card = self._card(metrics, padding=(16, 10))
            border.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
            ttk.Label(card, text=title, style="Note.TLabel").pack(anchor="w")
            ttk.Label(card, text=value, style="Card.TLabel", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(5, 4))
            ttk.Label(card, text=detail, style="Note.TLabel").pack(anchor="w")

        columns = ttk.Frame(outer, style="App.TFrame")
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=6, uniform="content")
        columns.columnconfigure(1, weight=5, uniform="content")
        left_border, card = self._card(columns)
        left_border.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right = ttk.Frame(columns, style="App.TFrame")
        right.grid(row=0, column=1, sticky="nsew")

        header = ttk.Frame(card, style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="服务连接", style="CardTitle.TLabel").pack(side="left")
        self.status_badge = ttk.Label(header, text="检查中", style="Note.TLabel")
        self.status_badge.pack(side="right")
        self.status_message = tk.StringVar()
        self.status_label = ttk.Label(card, textvariable=self.status_message, style="Note.TLabel", wraplength=440)
        self.status_label.pack(anchor="w", pady=(6, 12))
        self.values, self.entries = [], []
        for number, label, description in (("01", "Apify Token", "采集公开推文"),
                ("02", "DeepSeek API Key", "判断重置公告"), ("03", "QQ 邮箱授权码", "16 位授权码")):
            line = ttk.Frame(card, style="Card.TFrame")
            line.pack(fill="x", pady=(0, 6))
            ttk.Label(line, text=number, style="Note.TLabel", foreground=BLUE,
                      font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
            ttk.Label(line, text=label, style="Card.TLabel").pack(side="left")
            ttk.Label(line, text=description, style="Note.TLabel").pack(side="right")
            value = tk.StringVar()
            entry = ttk.Entry(card, textvariable=value, show="•", font=("Segoe UI", 10))
            entry.pack(fill="x", pady=(0, 12))
            self.values.append(value)
            self.entries.append(entry)
        self.reveal = tk.BooleanVar(value=False)
        self.reveal_button = ttk.Checkbutton(card, text="显示本次输入", variable=self.reveal,
                                            command=self.toggle_visibility, style="Card.TCheckbutton")
        self.reveal_button.pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        self.save_button = ttk.Button(row, text="保存连接", command=self.save, style="Primary.TButton")
        self.save_button.pack(side="left")
        self.clear_input_button = ttk.Button(row, text="清空输入", command=self.clear_inputs, style="Quiet.TButton")
        self.clear_input_button.pack(side="left", padx=(10, 0))
        ttk.Separator(card).pack(fill="x", pady=(14, 10))
        ttk.Label(card, text="凭据保存在本机 Windows 凭据管理器。\n保存后清空输入框，不回显已保存的密钥。",
                  style="Note.TLabel", wraplength=440).pack(anchor="w")
        self.delete_button = ttk.Button(card, text="移除已保存的凭据", command=self.clear_saved, style="Delete.TButton")
        self.delete_button.pack(anchor="w", pady=(2, 0))

        mail_border, mail = self._card(right, padding=(20, 17))
        mail_border.pack(fill="x")
        mail_header = ttk.Frame(mail, style="Card.TFrame")
        mail_header.pack(fill="x")
        ttk.Label(mail_header, text="邮件通知", style="CardTitle.TLabel").pack(side="left")
        self.address_button = ttk.Button(mail_header, text="显示地址", command=self.toggle_addresses, style="Link.TButton")
        self.address_button.pack(side="right")
        self.addresses_visible = False
        self.sender_message = tk.StringVar(value=mask_address(SENDER))
        self.recipient_message = tk.StringVar(value=mask_address(RECIPIENT))
        for label, variable in (("发件邮箱", self.sender_message), ("接收邮箱", self.recipient_message)):
            line = ttk.Frame(mail, style="Card.TFrame")
            line.pack(fill="x", pady=(10, 0))
            ttk.Label(line, text=label, style="Note.TLabel").pack(side="left")
            ttk.Label(line, textvariable=variable, style="Card.TLabel", font=("Segoe UI", 10)).pack(side="right")
        ttk.Label(mail, text="自动监控只通知相关公告。", style="Note.TLabel").pack(anchor="w", pady=(13, 0))

        test_border, test_card = self._card(right, padding=(20, 18))
        test_border.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(test_card, text="连接测试", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(test_card, text="验证从推文到邮箱的整条链路。", style="Note.TLabel").pack(anchor="w", pady=(6, 16))
        flow = ttk.Frame(test_card, style="Card.TFrame")
        flow.pack(fill="x")
        for index, label in enumerate(("采集 3 条", "批量分析", "测试邮件")):
            if index:
                ttk.Label(flow, text="  →  ", style="Note.TLabel").pack(side="left")
            tk.Label(flow, text=label, bg="#edf6f4", fg=BLUE, padx=7, pady=6,
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.test_button = ttk.Button(test_card, text="运行一次测试  →", command=self.start_test, style="Primary.TButton")
        self.test_button.pack(fill="x", pady=(18, 10))
        ttk.Label(test_card, text="会产生采集与模型用量，并发送一封测试邮件。", style="Note.TLabel", wraplength=370).pack(anchor="w")
        self.plan_message = tk.StringVar()
        ttk.Button(test_card, text="费用与运行说明", style="Link.TButton", command=self.show_plan).pack(anchor="w", pady=(3, 8))
        self.test_message = tk.StringVar(value="保存连接后，即可运行测试。")
        self.test_label = ttk.Label(test_card, textvariable=self.test_message, style="Note.TLabel", wraplength=370)
        self.test_label.pack(anchor="w", pady=(4, 0))

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.pack(fill="x", pady=(16, 0))
        self.activity_message = tk.StringVar(value="正在读取后台状态…")
        ttk.Label(footer, textvariable=self.activity_message, style="Subtitle.TLabel",
                  font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Label(footer, text="LOCAL FIRST  ·  PRIVATE BY DEFAULT", style="Subtitle.TLabel",
                  font=("Segoe UI", 8)).pack(side="right")
        self.refresh_background()

    def toggle_addresses(self):
        self.addresses_visible = not self.addresses_visible
        self.sender_message.set(SENDER if self.addresses_visible else mask_address(SENDER))
        self.recipient_message.set(RECIPIENT if self.addresses_visible else mask_address(RECIPIENT))
        self.address_button.configure(text="隐藏地址" if self.addresses_visible else "显示地址")

    def show_plan(self):
        messagebox.showinfo("本轮测试说明", self.plan_message.get(), parent=self.root)

    def refresh_background(self):
        try:
            status = self.status_loader()
            running = status.get("running") is True
            self.daemon_badge.configure(text=("●  " if running else "○  ") + status["title"],
                                        bg="#e5f3ed" if running else "#e8edf0", fg=GREEN if running else MUTED)
            self.activity_message.set(status["detail"] + "    ·    " + status["last_run"])
        except Exception:
            self.daemon_badge.configure(text="○  状态暂不可用", bg="#e8edf0", fg=MUTED)
            self.activity_message.set("后台状态读取失败；连接设置仍可使用。")
        self._status_after = self.root.after(15000, self.refresh_background)

    def apply_result(self, result):
        self.stored = result["stored"] is True
        self.status_message.set(result["message"])
        self.status_label.configure(foreground=RED if result["error"] else GREEN if self.stored else MUTED)
        self.status_badge.configure(text="已保存" if self.stored else "状态待确认" if result["stored"] is None else "未保存",
                                    foreground=GREEN if self.stored else MUTED)
        self.test_button.configure(state="normal" if self.stored and not self.busy else "disabled")

    def refresh(self):
        try:
            self.plan = self.plan_loader()
            self.plan_message.set(describe_test_plan(self.plan))
        except Exception:
            self.plan = {"ready": False}
            self.plan_message.set("累计预算或本轮费用方案无法校验，已停止；不会读取凭据或启动外部调用。")
        self.apply_result(self.controller.status())

    def _confirm_test(self, plan, run_id):
        return messagebox.askyesno("确认新一轮测试", describe_test_plan(plan, reveal_addresses=True) +
                                  "\n\n轮次：" + run_id +
                                  "\n确认按上述次数及费用说明授权并执行这一轮？仅本轮有效，不自动重试。", parent=self.root)

    def toggle_visibility(self):
        for entry in self.entries:
            entry.configure(show="" if self.reveal.get() else "•")

    def clear_inputs(self):
        for value in self.values:
            value.set("")
        self.reveal.set(False)
        self.toggle_visibility()

    def save(self):
        if self.busy:
            return False
        result = self.controller.save(*(value.get() for value in self.values))
        self.apply_result(result)
        if result["success"]:
            self.clear_inputs()
        return result["success"]

    def clear_saved(self):
        if self.busy:
            return
        result = self.controller.clear()
        self.apply_result(result)
        if result["success"]:
            self.clear_inputs()

    def start_test(self):
        if self.busy:
            return
        if any(value.get() for value in self.values) and not self.save():
            self.test_message.set("请补齐三项凭据并保存后再测试。")
            return
        self.refresh()
        if not self.stored:
            self.test_message.set("请先填写并保存三项凭据。")
            return
        if not self.plan["ready"]:
            self.test_label.configure(foreground=RED)
            self.test_message.set("尚未开始测试：\n" + test_blocking_reasons(self.plan) +
                                  "\n本次没有采集、分析或发送邮件。")
            return
        self.busy = True
        for button in (self.save_button, self.delete_button, self.test_button):
            button.configure(state="disabled")
        run_id = "manual-test-" + uuid4().hex
        try:
            confirmed = self.confirm_test(self.plan, run_id)
        except Exception:
            self.events.put(("done", "本轮确认未完成，测试未启动。", False))
            return
        if not confirmed:
            self.busy = False
            self.save_button.configure(state="normal")
            self.delete_button.configure(state="normal")
            self.test_message.set("已取消，本轮未运行。")
            self.refresh()
            return
        self.clear_inputs()
        self.test_label.configure(foreground=BLUE)
        self.test_message.set("正在启动本次测试…")
        threading.Thread(target=self.test_runner, args=(self.events, run_id), daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                self.test_message.set(event[1])
                if event[0] == "done":
                    self.busy = False
                    self.test_label.configure(foreground=GREEN if event[2] else RED)
                    self.save_button.configure(state="normal")
                    self.delete_button.configure(state="normal")
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def callback_error(self, *_):
        self.status_message.set("操作未完成，请重新打开设置窗口后重试。")
        self.status_label.configure(foreground=RED)

    def close(self):
        if self.busy:
            self.test_message.set("测试正在运行，请等待结果后关闭窗口。")
            return
        self.clear_inputs()
        self.root.destroy()


def main():
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
