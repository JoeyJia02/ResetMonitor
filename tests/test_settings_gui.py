"""Actual Tk widget checks with synthetic injected storage and fake test runner."""

from contextlib import ExitStack
import io
import queue
import time
import tkinter as tk
import unittest
from unittest.mock import patch


from monitor.local_settings import SENDER as LOCAL_SENDER, RECIPIENT as LOCAL_RECIPIENT
from monitor.settings_controller import SettingsController
from monitor.settings_gui import SettingsWindow, run_full_chain

RUN_ID = "manual-test-" + "a" * 32
FAKE_PLAN = {
    "totals": {"limit": "10.00", "max_requests": 9, "committed_cny": "0.001676", "requests": 1},
    "model_maximum_cny": "2.101248", "source_run_cap_usd": "0.0009",
    "credential_channel": "当前用户的 Windows 凭据管理器",
    "sender": LOCAL_SENDER, "recipient": LOCAL_RECIPIENT,
    "ready": True, "reasons": [],
}


class SettingsGUITests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.present = False
        self.deleted = 0
        def save(credentials):
            self.saved.append(credentials)
            self.present = True
        def clear():
            self.deleted += 1
            previous, self.present = self.present, False
            return previous
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        controller = SettingsController(saver=save, exists=lambda: self.present, deleter=clear)
        self.runs = 0
        self.run_ids = []
        self.confirmations = []
        def runner(events, run_id):
            self.runs += 1
            self.run_ids.append(run_id)
            events.put(("progress", "测试进度"))
            events.put(("done", "测试成功", True))
        def confirm(plan, run_id):
            self.confirmations.append((plan, run_id))
            return True
        self.app = SettingsWindow(self.root, controller=controller, test_runner=runner,
                                  plan_loader=lambda: dict(FAKE_PLAN), confirm_test=confirm,
                                  status_loader=lambda: {"running": False, "title": "演示",
                                                         "detail": "离线", "last_run": "尚未运行"})
        self.root.update_idletasks()

    def fill(self):
        for variable, value in zip(self.app.values, ("SYNTHETIC_GUI_APIFY", "SYNTHETIC_GUI_DEEPSEEK", "abcdefghijklmnop")):
            variable.set(value)

    def test_initial_masked_blank_fields_status_and_no_saved_secret_prefill(self):
        self.assertEqual([value.get() for value in self.app.values], ["", "", ""])
        self.assertEqual([entry.cget("show") for entry in self.app.entries], ["•"] * 3)
        self.assertIn("disabled", self.app.test_button.state())
        self.present = True
        self.app.refresh()
        self.assertEqual([value.get() for value in self.app.values], ["", "", ""])
        self.assertNotIn("disabled", self.app.test_button.state())

    def test_actual_manual_plan_is_ready_without_the_old_budget_or_cost_gate(self):
        from scripts.manual_test import manual_test_plan
        self.present = True
        self.app.plan_loader = manual_test_plan
        self.app.refresh()
        self.assertTrue(self.app.plan['manual'])
        self.assertTrue(self.app.plan['ready'])
        self.assertNotIn('disabled', self.app.test_button.state())
        self.assertIn('不检查累计预算', self.app.plan_message.get())

    def test_email_addresses_are_masked_until_explicitly_shown(self):
        from monitor.local_settings import mask_address
        self.assertEqual(self.app.sender_message.get(), mask_address(LOCAL_SENDER))
        self.assertEqual(self.app.recipient_message.get(), mask_address(LOCAL_RECIPIENT))
        self.assertNotIn(LOCAL_RECIPIENT, self.app.plan_message.get())
        self.app.toggle_addresses()
        self.assertEqual(self.app.recipient_message.get(), LOCAL_RECIPIENT)
        self.app.toggle_addresses()
        self.assertEqual(self.app.recipient_message.get(), mask_address(LOCAL_RECIPIENT))

    def test_save_and_clear_inputs_rehide_and_enable_test(self):
        self.fill()
        self.app.reveal.set(True)
        self.app.toggle_visibility()
        self.assertEqual(self.app.entries[0].cget("show"), "")
        self.assertTrue(self.app.save())
        self.assertEqual(len(self.saved), 1)
        self.assertEqual([value.get() for value in self.app.values], ["", "", ""])
        self.assertEqual(self.app.entries[0].cget("show"), "•")
        self.assertNotIn("disabled", self.app.test_button.state())
        self.assertNotIn("SYNTHETIC", self.app.status_message.get())

    def test_invalid_input_keeps_fields_without_save_or_test(self):
        self.app.values[0].set("SYNTHETIC_PARTIAL")
        self.app.start_test()
        self.assertEqual(self.saved, [])
        self.assertEqual(self.runs, 0)
        self.assertEqual(self.app.values[0].get(), "SYNTHETIC_PARTIAL")

    def test_clear_saved_updates_status_and_disables_test(self):
        self.fill()
        self.app.save()
        self.app.clear_saved()
        self.assertEqual(self.deleted, 1)
        self.assertFalse(self.present)
        self.assertIn("disabled", self.app.test_button.state())

    def test_test_button_saves_new_inputs_and_runs_once_with_thread_events(self):
        self.fill()
        self.app.start_test()
        self.assertEqual(len(self.saved), 1)
        for _ in range(100):
            if not self.app.events.empty():
                self.app.poll()
                if not self.app.busy:
                    break
            time.sleep(0.001)
        self.assertEqual(self.runs, 1)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.test_message.get(), "测试成功")

    def test_callback_errors_are_fixed_and_close_waits_while_busy(self):
        self.app.callback_error(Exception, RuntimeError("SYNTHETIC_SECRET"), None)
        self.assertNotIn("SYNTHETIC_SECRET", self.app.status_message.get())
        self.app.busy = True
        self.app.close()
        self.assertTrue(self.root.winfo_exists())
        self.assertIn("测试正在运行", self.app.test_message.get())
        self.app.busy = False

    def test_each_confirmed_click_gets_a_new_run_and_duplicate_click_is_ignored(self):
        self.fill()
        with patch("monitor.settings_gui.threading.Thread") as thread:
            self.app.start_test()
            self.app.start_test()
            self.assertEqual(thread.call_count, 1)
            first = thread.call_args.kwargs["args"][1]
            self.assertRegex(first, r"^manual-test-[0-9a-f]{32}$")
            self.app.events.put(("done", "完成", True))
            self.app.poll()
            self.app.start_test()
            second = thread.call_args.kwargs["args"][1]
            self.assertNotEqual(first, second)
            self.assertEqual(len(self.confirmations), 2)

    def test_cancelled_confirmation_does_not_start_a_child_or_change_history(self):
        self.fill()
        self.app.save()
        self.app.confirm_test = lambda *_: False
        with patch("monitor.settings_gui.threading.Thread") as thread:
            self.app.start_test()
        thread.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertIn("已取消", self.app.test_message.get())
        self.assertIn("0.001676", self.app.plan_message.get())

    def test_budget_and_cost_refusal_displayed_before_confirmation_or_child(self):
        self.present = True
        self.app.plan_loader = lambda: dict(FAKE_PLAN, ready=False, reasons=[
            "MODEL_BUDGET_REQUEST_LIMIT_REACHED", "FULL_CHAIN_SOURCE_COST_UNVERIFIED"])
        self.app.refresh()
        with patch("monitor.settings_gui.threading.Thread") as thread:
            self.app.start_test()
        thread.assert_not_called()
        self.assertEqual(self.confirmations, [])
        self.assertNotIn("disabled", self.app.test_button.state())
        self.assertIn("次数已用完", self.app.plan_message.get())
        self.assertIn("完整费用上界尚未核定", self.app.plan_message.get())
        self.assertIn("2.101248", self.app.plan_message.get())
        self.assertIn("Windows 凭据管理器", self.app.plan_message.get())
        self.assertIn("次数已用完", self.app.test_message.get())
        self.assertIn("完整费用上界尚未核定", self.app.test_message.get())
        self.assertIn("本次没有采集、分析或发送邮件", self.app.test_message.get())


class GUIRunnerTests(unittest.TestCase):
    def test_detailed_tls_failure_preserves_saved_result_guidance(self):
        for count, code in ((0, "SMTP_CONNECT_TLS_PEER_CLOSED"),
                            (3, "SMTP_AUTH_TLS_ERROR"),
                            (3, "SMTP_CONNECT_TLS_CERTIFICATE_INVALID")):
            with self.subTest(count=count, code=code):
                events = queue.Queue()
                process = type('Child', (), {
                    'stdout': io.StringIO(f'FAILED n={count} email=FAILED {code}\n'),
                    'wait': lambda _: 1})()
                with patch('monitor.settings_gui.subprocess.Popen', return_value=process) as start:
                    run_full_chain(events, RUN_ID)
                self.assertEqual(start.call_count, 1)
                done = events.get_nowait()
                self.assertIn('结果已保存', done[1])
                self.assertIn('安全连接失败', done[1])
                self.assertFalse(done[2])

    def test_unsupported_smtp_phase_preserves_saved_result_guidance(self):
        for count, code in ((0, "SMTP_AUTH_UNSUPPORTED"),
                            (3, "SMTP_AUTH_UNSUPPORTED"),
                            (3, "SMTP_STARTTLS_UNSUPPORTED")):
            with self.subTest(count=count, code=code):
                events = queue.Queue()
                process = type('Child', (), {
                    'stdout': io.StringIO(f'FAILED n={count} email=FAILED {code}\n'),
                    'wait': lambda _: 1})()
                with patch('monitor.settings_gui.subprocess.Popen', return_value=process) as start:
                    run_full_chain(events, RUN_ID)
                self.assertEqual(start.call_count, 1)
                done = events.get_nowait()
                self.assertIn('结果已保存', done[1])
                self.assertIn('请勿重新采集或分析', done[1])
                self.assertIn('本轮没有可分析推文' if count == 0 else '分析已完成', done[1])
                self.assertFalse(done[2])

    def test_saved_analysis_email_connection_failure_is_reported_without_rerun(self):
        events = queue.Queue()
        process = type('Child', (), {'stdout':io.StringIO('FAILED n=3 email=FAILED SMTP_TLS_OR_AUTH_UNSUPPORTED\n'),
                                    'wait':lambda _:1})()
        with patch('monitor.settings_gui.subprocess.Popen', return_value=process) as start:
            run_full_chain(events, RUN_ID)
        self.assertEqual(start.call_count, 1)
        done = events.get_nowait()
        self.assertEqual(done[0], 'done')
        self.assertIn('分析已完成', done[1])
        self.assertIn('结果已保存', done[1])
        self.assertFalse(done[2])

    def test_budget_refusal_does_not_request_new_credentials_or_repeat_child(self):
        events = queue.Queue()
        process = type('Child', (), {'stdout':io.StringIO('MODEL_BUDGET_LIMIT_EXCEEDED\n'), 'wait':lambda _:2})()
        with patch('monitor.settings_gui.subprocess.Popen', return_value=process) as start:
            run_full_chain(events, RUN_ID)
        self.assertEqual(start.call_count, 1)
        done = events.get_nowait()
        self.assertIn('未启动新的采集或分析', done[1])
        self.assertFalse(done[2])

    def test_hidden_child_no_secret_arguments_and_fixed_safe_progress(self):
        events = queue.Queue()
        process = type("Child", (), {"stdout": io.StringIO("RAW_SECRET_SHOULD_NOT_BE_SHOWN\n[1/3] ignored\nFULL_CHAIN_OK n=3 email=SENT\n"),
                                     "wait": lambda _: 0})()
        with patch("monitor.settings_gui.subprocess.Popen", return_value=process) as start:
            run_full_chain(events, RUN_ID)
        args, options = start.call_args
        self.assertEqual(args[0][-4:], ["--run", "--run-id", RUN_ID, "--confirmed"])
        self.assertIn("manual_test.py", args[0][-5])
        self.assertNotIn("token", " ".join(args[0]))
        self.assertTrue(options["creationflags"])
        delivered = []
        while not events.empty():
            delivered.append(events.get_nowait())
        self.assertEqual(delivered[-1][0], "done")
        self.assertTrue(delivered[-1][2])
        self.assertNotIn("RAW_SECRET", repr(delivered))


if __name__ == "__main__":
    unittest.main()
