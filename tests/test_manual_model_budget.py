"""Real isolated ledger/files/OS locks; every provider and credential IO is fake."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from monitor import manual_model_budget as budget
from scripts import analyze_recovered_three as batch
from scripts import test_full_chain_once as chain
from tests.test_classification_setup_probe import FakeResponse, FakeTransport, response_document
from tests.test_batch import child_environment

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get('RESET_MONITOR_TEST_ROOT', str(ROOT / 'runtime/test-tmp'))).resolve()
SHA = '0' * 64

class ManualModelBudgetTests(unittest.TestCase):
    def test_public_summary_preserves_historical_usage_and_witness_without_a_lock(self):
        self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA)
        Path(str(self.path) + '.lock').unlink()
        before = {p.name:p.read_bytes() for p in self.directory.iterdir()}
        self.assertEqual(self.ledger.summary(), {'limit':'1.00', 'max_requests':1,
                                               'committed_cny':'0.001676', 'requests':1})
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.directory.iterdir()})

    def test_new_id_and_new_directory_never_renew_consumed_global_approval(self):
        self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA)
        before = (self.path.read_bytes(), self.ledger.witness.read_bytes())
        with patch.object(batch, 'RESERVED_MAXIMUM_CNY', Decimal('.1')):
            plan = chain.new_run_plan(model_budget=self.ledger,
                                      clock=lambda:datetime(2026,10,11,11,tzinfo=timezone.utc))
            self.assertEqual(plan['totals']['committed_cny'], '0.001676')
            self.assertNotIn('MODEL_BUDGET_REQUEST_LIMIT_REACHED', plan['reasons'])
            self.assertIn('FULL_CHAIN_SOURCE_COST_UNVERIFIED', plan['warnings'])
            self.assertTrue(plan['ready'])
            for _ in range(2):
                run_id = 'full-chain-' + uuid4().hex
                target = self.directory / run_id
                output = io.StringIO()
                result = chain.main(['--run', '--run-id', run_id, '--confirmed'], output=output,
                    clock=lambda:datetime(2026,10,11,11,tzinfo=timezone.utc), directory=target,
                    model_budget=self.ledger, credential_loader=lambda:self.fail('NO_CREDENTIAL_IO'),
                    source_fetch=lambda *_:self.fail('NO_SOURCE_IO'))
                self.assertEqual(result, 2)
                self.assertIn('MODEL_BUDGET_REQUEST_LIMIT_REACHED', output.getvalue())
                self.assertFalse(target.exists())
        self.assertEqual(before, (self.path.read_bytes(), self.ledger.witness.read_bytes()))

    def test_read_only_inspection_creates_no_lock_or_writes(self):
        self.ledger.initialize()
        lock = Path(str(self.path) + '.lock')
        lock.unlink()
        before = {p.name:p.read_bytes() for p in self.directory.iterdir()}
        self.ledger.inspect(Decimal('.1'))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.directory.iterdir()})
        self.assertFalse(lock.exists())

    def setUp(self):
        self.directory = TEST_ROOT / ('manual-budget-' + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.path = self.directory / 'shared.json'
        self.ledger = budget.ManualModelBudget(self.path)
        for name in ('socket.create_connection','smtplib.SMTP','smtplib.SMTP_SSL'):
            guard = patch(name, side_effect=AssertionError('REAL_IO_FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)

    def cleanup(self):
        if not self.directory.resolve().is_relative_to(TEST_ROOT.resolve()):
            raise AssertionError('UNSAFE_TEST_CLEANUP')
        shutil.rmtree(self.directory)

    def assert_code(self, code, action):
        with self.assertRaises(budget.ModelBudgetError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_missing_ledger_is_not_automatically_zero_initialized(self):
        self.assert_code('MODEL_BUDGET_NOT_INITIALIZED', lambda:self.ledger.reserve(Decimal('.1'), request_sha256=SHA))
        self.assertFalse(self.path.exists())

    def test_existing_usage_is_imported_once_and_never_reset(self):
        first = self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA)
        again = self.ledger.initialize()
        self.assertEqual(first, again)
        self.assertEqual(again['requests'], 1)
        self.assertEqual(again['committed_cny'], '0.001676')
        self.assert_code('MODEL_BUDGET_REQUEST_LIMIT_REACHED', lambda:self.ledger.reserve(Decimal('.1'), request_sha256=SHA))

    def test_existing_empty_ledger_cannot_silently_ignore_historical_import(self):
        self.ledger.initialize()
        before = (self.path.read_bytes(), self.ledger.witness.read_bytes())
        self.assert_code('MODEL_BUDGET_MIGRATION_RECONCILIATION_REQUIRED',
            lambda:self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA))
        self.assertEqual(before, (self.path.read_bytes(), self.ledger.witness.read_bytes()))

    def test_same_explicit_history_is_idempotent_without_rewriting_ledger(self):
        first = self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA)
        before = (self.path.read_bytes(), self.ledger.witness.read_bytes())
        again = self.ledger.initialize(historical_amount=Decimal('.001676'), historical_receipt_sha256=SHA)
        self.assertEqual(first, again)
        self.assertEqual(before, (self.path.read_bytes(), self.ledger.witness.read_bytes()))

    def test_conflicting_history_or_existing_reservation_is_never_refunded(self):
        self.ledger.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA)
        before = (self.path.read_bytes(), self.ledger.witness.read_bytes())
        for amount, reference in (('0.001','0'*64), ('0.001676','1'*64)):
            self.assert_code('MODEL_BUDGET_MIGRATION_RECONCILIATION_REQUIRED',
                lambda:self.ledger.initialize(historical_amount=amount, historical_receipt_sha256=reference))
        self.assertEqual(before, (self.path.read_bytes(), self.ledger.witness.read_bytes()))
        reserved = budget.ManualModelBudget(self.directory / 'already-reserved.json')
        reserved.initialize()
        reserved.reserve(Decimal('.75'), request_sha256=SHA)
        self.assert_code('MODEL_BUDGET_MIGRATION_RECONCILIATION_REQUIRED',
            lambda:reserved.initialize(historical_amount='0.001676', historical_receipt_sha256=SHA))
        self.assertEqual(reserved.snapshot()['committed_cny'], '0.75')

    def test_remaining_money_is_checked_before_post(self):
        self.ledger.initialize(historical_amount='0.70', historical_receipt_sha256=SHA)
        self.assert_code('MODEL_BUDGET_LIMIT_EXCEEDED', lambda:self.ledger.reserve(Decimal('.31'), request_sha256=SHA))
        self.assertEqual(self.ledger.snapshot()['committed_cny'], '0.70')

    def test_unknown_or_larger_bound_is_rejected_without_creating_ledger(self):
        for amount, code in ((Decimal('2.101248'),'MODEL_BUDGET_LIMIT_EXCEEDED'),
                             (Decimal(0),'MODEL_BUDGET_BOUND_UNVERIFIED'),
                             (None,'MODEL_BUDGET_AMOUNT_INVALID')):
            self.assert_code(code, lambda:self.ledger.preflight(amount))
        self.assertFalse(self.path.exists())

    def test_fsynced_reservation_survives_restart_and_new_output_directory(self):
        self.ledger.initialize()
        self.ledger.reserve(Decimal('.6'), request_sha256=SHA)
        reopened = budget.ManualModelBudget(self.path)
        self.assertEqual(reopened.snapshot()['committed_cny'], '0.6')
        for directory in ('root-a','root-b'):
            output = io.StringIO()
            with patch.object(batch, 'RESERVED_MAXIMUM_CNY', Decimal('.6')), patch.object(budget, 'fixed_path', return_value=self.path):
                result = chain.main(['--run'], output=output, clock=lambda:datetime(2026,10,11,11,tzinfo=timezone.utc),
                    directory=self.directory / directory, credential_loader=lambda:self.fail('NO_CREDENTIAL_IO'),
                    source_fetch=lambda *args:self.fail('NO_SOURCE_IO'))
            self.assertEqual(result, 2)
            self.assertIn('MODEL_BUDGET_LIMIT_EXCEEDED', output.getvalue())
            self.assertFalse((self.directory / directory).exists())

    def test_default_full_chain_bound_stops_before_credentials_source_or_model(self):
        output = io.StringIO()
        with patch.object(budget, 'fixed_path', return_value=self.path):
            result = chain.main(['--run'], output=output, clock=lambda:datetime(2026,10,11,11,tzinfo=timezone.utc),
                directory=self.directory / 'output', credential_loader=lambda:self.fail('NO_CREDENTIAL_IO'),
                source_fetch=lambda *args:self.fail('NO_SOURCE_IO'))
        self.assertEqual(result, 2)
        self.assertIn('MODEL_BUDGET_LIMIT_EXCEEDED', output.getvalue())
        self.assertFalse((self.directory / 'output').exists())

    def test_default_sample_entry_bound_stops_before_sample_and_secret(self):
        output = io.StringIO()
        with patch.object(budget, 'fixed_path', return_value=self.path):
            result = batch.main(['--analyze-one-batch'], output=output,
                clock=lambda:datetime(2026,10,11,11,tzinfo=timezone.utc), directory=self.directory / 'output',
                sample_loader=lambda:self.fail('NO_SAMPLE_IO'), hidden_input=lambda _:self.fail('NO_SECRET_IO'))
        self.assertEqual(result, 2)
        self.assertIn('MODEL_BUDGET_LIMIT_EXCEEDED', output.getvalue())

    def test_direct_analyze_cannot_bypass_real_budget(self):
        transport = FakeTransport(FakeResponse(response_document()))
        with patch.object(budget, 'fixed_path', return_value=self.path):
            with self.assertRaises(batch._LocalFailure) as raised:
                batch._analyze('SYNTHETIC_KEY',b'{}',transport,{}, {},input_ids=['1','2','3'])
        self.assertEqual(raised.exception.code, 'MODEL_BUDGET_LIMIT_EXCEEDED')
        self.assertFalse(transport.calls)

    def test_missing_usage_failure_does_not_refund_reservation(self):
        self.ledger.initialize()
        document = response_document()
        document.pop('usage')
        transport = FakeTransport(FakeResponse(document))
        with patch.object(batch, 'RESERVED_MAXIMUM_CNY', Decimal('.75')):
            with self.assertRaises(batch._LocalFailure):
                batch._analyze('SYNTHETIC_KEY',b'{}',transport,{}, {},input_ids=['1','2','3'], model_budget=self.ledger)
            self.assertEqual(self.ledger.snapshot()['committed_cny'], '0.75')
            with self.assertRaises(batch._LocalFailure) as raised:
                batch._analyze('SYNTHETIC_KEY',b'{}',transport,{}, {},input_ids=['1','2','3'], model_budget=budget.ManualModelBudget(self.path))
        self.assertEqual(raised.exception.code, 'MODEL_BUDGET_LIMIT_EXCEEDED')
        self.assertEqual(len(transport.calls), 1)

    def test_deleted_main_file_and_partial_commit_do_not_restore_money(self):
        self.ledger.initialize()
        self.ledger.reserve(Decimal('.4'), request_sha256=SHA)
        self.path.unlink()
        self.assert_code('MODEL_BUDGET_RECOVERY_REQUIRED', lambda:self.ledger.initialize())

    def test_interrupted_commit_keeps_witness_and_refuses_recovery(self):
        self.ledger.initialize()
        original = self.ledger._atomic
        def fail_main(path, raw):
            if path == self.path:
                raise OSError('SYNTHETIC_FAILURE')
            return original(path,raw)
        with patch.object(self.ledger, '_atomic', side_effect=fail_main):
            self.assert_code('MODEL_BUDGET_COMMIT_FAILED', lambda:self.ledger.reserve(Decimal('.4'), request_sha256=SHA))
        self.assert_code('MODEL_BUDGET_RECOVERY_REQUIRED', lambda:self.ledger.snapshot())

    def test_two_processes_cannot_double_debit_or_call(self):
        self.ledger.initialize()
        child_code = '''
import sys,time,pathlib
from decimal import Decimal
from monitor.manual_model_budget import ManualModelBudget,ModelBudgetError
p=pathlib.Path(sys.argv[1]); gate=pathlib.Path(sys.argv[2]); result=pathlib.Path(sys.argv[3])
while not gate.exists(): time.sleep(.01)
try:
 ManualModelBudget(p).reserve(Decimal('.6'),request_sha256='0'*64)
 result.write_text('FAKE_POST_ONCE'); print('RESERVED')
except ModelBudgetError as error: print(error.code)
'''
        gate = self.directory / 'start'
        children = [subprocess.Popen([sys.executable,'-B','-c',child_code,str(self.path),str(gate),str(self.directory / ('post'+str(i)))],
            cwd=ROOT, env=child_environment(), stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(2)]
        gate.write_text('start')
        outputs = [child.communicate(timeout=15) for child in children]
        self.assertTrue(all(child.returncode == 0 for child in children), outputs)
        self.assertEqual(sum('RESERVED' in out for out,_ in outputs), 1)
        self.assertEqual(sum((self.directory / ('post'+str(i))).exists() for i in range(2)), 1)
        self.assertEqual(self.ledger.snapshot()['committed_cny'], '0.6')
