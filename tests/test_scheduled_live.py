"""Actual finite CLI/Monitor/guards with fake wire and isolated public ledgers.

Semantic multi-round cases grant nine *synthetic* requests in their private
ledger. The separate historical-approval case retains production's one-request
limit. Neither test capabilities nor fixtures install production proof entries.
"""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import smtplib
import unittest
from unittest.mock import patch
from uuid import uuid4

import main

from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.batch import BatchAuthorization, BatchLedger
from monitor.credentials import ProjectCredentials
from monitor.live_cli import LiveDependencies
from monitor.locking import LockBusy, ProcessLock
from monitor.manual_model_budget import ManualModelBudget
from monitor.state import StateStore, initial_state
from tests.test_apify import FakeHTTP, page, response, tweet
from tests.test_deepseek import QueueTransport, completion, response as model_response
from tests.test_live_cli import NOW, synthetic_live_inputs
from tests.test_smtp_transport import FakeFactory, FakeSession

ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = Path(os.environ.get('RESET_MONITOR_INTEGRATION_TEST_ROOT') or
                 ROOT / 'runtime' / 'test-tmp').resolve()
CASE_RECEIPTS = []
SYNTHETIC_SECRET = 'offline-scheduled-credential-no-real-account'


class ScheduledLiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = CASE_ROOT / ('scheduled-live-' + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.now = NOW
        self.raw, self.settings, self.sources, self.tokens, self.costs = synthetic_live_inputs(self.directory)
        self.raw['expires_at'] = (NOW + timedelta(hours=23)).isoformat()
        self.raw['deepseek'].update(max_requests=9, amount='0.20')
        self.raw['apify']['amount'] = '0.10'
        self.raw['sender'] = LOCAL_SENDER
        self.settings['smtp_host'] = 'smtp.qq.com'
        self.sources = {key: replace(value, expires_at=(NOW + timedelta(hours=23)).isoformat())
                        for key, value in self.sources.items()}
        self.costs = {key: replace(value, expires_at=NOW + timedelta(hours=23))
                      for key, value in self.costs.items()}
        self.auth_file = self.directory / 'public authorization.json'
        self.config_file = self.directory / 'public configuration.json'
        self.write_inputs()
        self.authorization = BatchAuthorization.from_dict(self.raw, now=NOW)
        self.batch = BatchLedger(Path(self.raw['ledger_path']), self.authorization, clock=lambda: self.now)
        self.batch.initialize()
        self.shared_patch = patch('monitor.manual_model_budget.MAX_REQUESTS', 9)
        self.shared_patch.start()
        self.addCleanup(self.shared_patch.stop)
        self.shared = ManualModelBudget(self.directory / 'synthetic-shared-budget.json')
        self.shared.initialize()
        self.store = StateStore(Path(self.raw['state_path']))
        state = initial_state('thsottiaux', execution_mode='LIVE', target_user_id='12345')
        state.update(initialized=True, initial_baseline_id='100', last_seen_id='100')
        self.store.save(state)
        self.apify, self.model, self.smtp = FakeHTTP(), QueueTransport(), FakeFactory()
        self.effects, self.outputs, self.boundary_proofs = [], [], []
        for name in ('socket.socket', 'socket.create_connection', 'smtplib.SMTP', 'smtplib.SMTP_SSL',
                     'monitor.credential_store.load_credentials'):
            guard = patch(name, side_effect=AssertionError('REAL_BOUNDARY_FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)
        # Capture committed state at every fake-wire boundary, using real files
        # and the OS lock implementation rather than a fake reservation method.
        for service, target in (('apify', self.apify), ('model', self.model)):
            original = target.request
            def checked(*args, _service=service, _original=original, **kwargs):
                self.verify_boundary(_service, kwargs.get('body'))
                return _original(*args, **kwargs)
            target.request = checked
        self.addCleanup(self.finish_case)

    def write_inputs(self):
        self.auth_file.write_text(json.dumps(self.raw), encoding='utf-8')
        self.config_file.write_text(json.dumps(self.settings), encoding='utf-8')

    def verify_boundary(self, service, body=None):
        with self.assertRaises(LockBusy):
            with ProcessLock(str(self.store.path) + '.lock'):
                pass
        finite = self.batch.validate_ready()['counts']
        if service == 'model':
            shared = self.shared.snapshot()
            expected = len(self.model.calls) + 1
            self.assertEqual(finite['deepseek_request'], expected)
            self.assertEqual(shared['requests'], expected)
            self.assertEqual(shared['entries'][-1]['reference_sha256'], hashlib.sha256(body).hexdigest())
        elif service == 'smtp':
            self.assertEqual(finite['smtp_attempt'], len(self.smtp.calls) + 1)
            self.assertTrue(any(value['status'] == 'SENDING'
                                for value in self.store.load()['pending_notifications'].values()))
        else:
            self.assertEqual(finite['apify_http_attempts'], len(self.apify.calls) + 1)
        self.boundary_proofs.append(service)

    def dependencies(self, **changes):
        def credentials(source):
            self.effects.append('credential:' + source)
            return ProjectCredentials(apify_token=SYNTHETIC_SECRET, deepseek_api_key=SYNTHETIC_SECRET,
                                      smtp_user=self.raw['sender'], smtp_password=SYNTHETIC_SECRET)
        def smtp(*args, **kwargs):
            self.verify_boundary('smtp')
            return self.smtp(*args, **kwargs)
        values = dict(clock=lambda: self.now, credential_reader=credentials,
                      apify_transport_factory=lambda: self.apify,
                      deepseek_transport_factory=lambda: self.model, smtp_factory=smtp,
                      sleep=lambda _seconds: None, trusted_ledger_path=Path(self.raw['ledger_path']),
                      source_catalog=self.sources, token_proofs=self.tokens, apify_cost_proofs=self.costs,
                      model_budget_factory=lambda: self.shared)
        values.update(changes)
        return LiveDependencies(**values)

    def invoke(self, *, scheduled=True, source='stored', extra=(), **dependency_changes):
        args = ['--live', '--authorization', str(self.auth_file), '--live-config', str(self.config_file)]
        if scheduled:
            args.append('--scheduled-validation')
        if source is not None:
            args += ['--credential-source', source]
        args.extend(extra)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main.main(args, live_dependencies=self.dependencies(**dependency_changes), project_root=ROOT)
        self.assertNotIn(SYNTHETIC_SECRET, output.getvalue())
        result = json.loads(output.getvalue())
        self.outputs.append(dict(result, exit_code=code))
        return result, code

    def queue_round(self, identifiers, labels=()):
        count = sum(call['method'] == 'POST' for call in self.apify.calls) + 1
        self.run_id = str(count) * 17
        self.dataset_id = ('D' if count % 2 else 'E') * 17
        data = dict(id=self.run_id, status='RUNNING', defaultDatasetId=self.dataset_id, buildId='B' * 17)
        self.apify.actions.extend([response(201, {'data': data}),
            response(200, {'data': dict(data, status='SUCCEEDED')}),
            page([tweet(value, authorId='12345') for value in identifiers])])
        self.model.outcomes.extend(model_response(completion(label)) for label in labels)

    def finish_case(self):
        CASE_RECEIPTS.append(dict(case=self._testMethodName, case_root=str(self.directory),
            entry_point='D:/ResetMonitor/main.py --live --scheduled-validation',
            actual_monitor_and_guarded_adapters=True, synthetic_private_proofs=True,
            synthetic_shared_max_requests=9, production_max_requests_unchanged=1,
            fake_apify_calls=len(self.apify.calls), fake_model_calls=len(self.model.calls),
            fake_smtp_attempts=len(self.smtp.calls), committed_boundary_checks=self.boundary_proofs,
            outputs=self.outputs, real_external_calls=0, actual_task_registered=False))
        if os.environ.get('RESET_MONITOR_KEEP_INTEGRATION_EVIDENCE') != '1':
            target = self.directory.resolve()
            if target == CASE_ROOT or target.parent != CASE_ROOT:
                raise AssertionError('Unsafe fixture cleanup')
            shutil.rmtree(target)

    def test_first_round_sets_baseline_without_historical_classification_or_mail(self):
        state = self.store.load()
        state.update(initialized=False, initial_baseline_id=None, last_seen_id=None)
        self.store.save(state)
        self.queue_round([103, 102, 101])
        result, code = self.invoke()
        self.assertEqual((code, result['classified'], result['sent']), (0, 0, 0))
        self.assertEqual(self.store.load()['initial_baseline_id'], '103')
        self.assertEqual((len(self.model.calls), len(self.smtp.calls)), (0, 0))

    def test_three_alert_labels_and_next_round_duplicates_keep_finite_source_quota(self):
        self.queue_round([103, 102, 101], ['PRE_RESET', 'POST_RESET', 'POSSIBLE_RESET'])
        result, code = self.invoke()
        self.assertEqual((code, result['classified'], result['sent']), (0, 3, 3))
        state = self.store.load()
        self.assertEqual([state['processed_ids'][str(value)]['classification']['label'] for value in (101, 102, 103)],
                         ['PRE_RESET', 'POST_RESET', 'POSSIBLE_RESET'])
        subjects = [str(BytesParser(policy=policy.default).parsebytes(data)['Subject'])
                    for data in self.smtp.session.payloads]
        for word in ('预告', '已重置', '疑似'):
            self.assertTrue(any(word in subject for subject in subjects), subjects)
        for message in self.smtp.session.payloads:
            self.assertEqual(BytesParser(policy=policy.default).parsebytes(message)['To'], self.raw['recipient'])
        self.assertTrue(all(call[1][0] == 'LOGIN' for call in self.smtp.session.calls if call[0] == 'auth'))
        repeat, code = self.invoke()
        self.assertEqual((code, repeat['classified'], repeat['sent']), (1, 0, 0))
        self.assertEqual((len(self.apify.calls), len(self.model.calls), len(self.smtp.calls)), (3, 3, 3))

    def test_irrelevant_creates_no_email(self):
        self.queue_round([101], ['IRRELEVANT'])
        irrelevant, code = self.invoke()
        self.assertEqual((code, irrelevant['classified'], irrelevant['sent']), (0, 1, 0))
        self.assertEqual((len(self.model.calls), len(self.smtp.calls)), (1, 0))

    def test_duplicate_source_batch_is_refused_without_classification_or_mail(self):
        self.queue_round([102, 101, 101], ['PRE_RESET', 'IRRELEVANT'])
        result, code = self.invoke()
        self.assertEqual((code, result['ingested'], result['classified'], result['sent']), (1, 0, 0, 0))
        self.assertEqual(self.store.load()['processed_ids'], {})
        self.assertEqual(self.store.load()['active_fetch']['last_error'], 'APIFY_TWEET_CONTRACT_ERROR')

    def test_source_api_temporary_failure_resumes_same_run_without_another_paid_start(self):
        self.queue_round([101], ['POST_RESET'])
        self.apify.actions[1] = response(503, {}, {'Retry-After': '30'})
        self.apify.actions.insert(2, response(200, {'data': dict(id=self.run_id, status='SUCCEEDED',
            defaultDatasetId=self.dataset_id, buildId='B' * 17)}))
        result, code = self.invoke()
        self.assertEqual((code, result['status'], len(self.model.calls)), (1, 'DEGRADED', 0))
        self.assertEqual(self.store.load()['active_fetch']['run_id'], self.run_id)
        self.now += timedelta(seconds=31)
        result, code = self.invoke()
        self.assertEqual((code, result['classified'], result['sent']), (0, 1, 1))
        self.assertEqual([call['method'] for call in self.apify.calls], ['POST', 'GET', 'GET', 'GET'])
        self.assertIsNone(self.store.load()['active_fetch'])

    def test_model_api_failure_keeps_reservations_and_recovers_saved_inbox(self):
        self.queue_round([101])
        self.model.outcomes.extend([model_response(status=503)] * 3)
        result, code = self.invoke()
        self.assertEqual((code, result['status'], len(self.model.calls)), (1, 'DEGRADED', 3))
        self.assertIn('101', self.store.load()['inbox'])
        before = self.shared.snapshot()
        self.assertEqual(before['requests'], 3)
        self.now += timedelta(hours=2, seconds=1)
        self.model.outcomes.append(model_response(completion('POSSIBLE_RESET')))
        result, code = self.invoke()
        # Fetching another paid run remains blocked by the same finite envelope;
        # recovery of the already persisted inbox can still finish locally.
        self.assertEqual((code, result['classified'], result['sent']), (1, 1, 1))
        self.assertEqual(len(self.apify.calls), 3)
        after = self.shared.snapshot()
        self.assertEqual(after['requests'], 4)
        self.assertGreater(Decimal(after['committed_cny']), Decimal(before['committed_cny']))
        self.assertEqual(self.store.load()['inbox'], {})

    def test_smtp_unknown_is_not_repeated_during_cooldown_or_expired_authorization(self):
        self.smtp = FakeFactory(FakeSession(getreply=smtplib.SMTPServerDisconnected('synthetic lost ack')))
        self.queue_round([101], ['PRE_RESET'])
        result, code = self.invoke()
        self.assertEqual((code, result['status'], result['sent']), (1, 'DEGRADED', 0))
        notice = next(iter(self.store.load()['pending_notifications'].values()))
        self.assertEqual((notice['status'], notice['unknown_retry_used']), ('UNKNOWN', 0))
        self.assertEqual(notice['next_retry_at'], (NOW + timedelta(hours=24)).isoformat())
        self.invoke()
        counts = (len(self.apify.calls), len(self.model.calls), len(self.smtp.calls))
        self.now += timedelta(hours=24)
        result, code = self.invoke()
        self.assertEqual((code, result['status']), (2, 'REFUSED'))
        self.assertIn('VALIDATION_AUTHORIZATION_EXPIRED', result['issue_codes'])
        self.assertEqual((len(self.apify.calls), len(self.model.calls), len(self.smtp.calls)), counts)
        self.assertEqual(counts[1:], (1, 1))

    def test_overlapping_round_skips_all_fake_wire_and_state_changes(self):
        before = self.store.path.read_bytes()
        with ProcessLock(str(self.store.path) + '.lock'):
            result, code = self.invoke()
        self.assertEqual((code, result['status']), (0, 'LOCKED'))
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.boundary_proofs, [])

    def test_scheduled_entry_requires_stored_credentials_and_forbids_ledger_initialization(self):
        for source in ('env', 'prompt', None):
            result, code = self.invoke(source=source)
            self.assertEqual((code, result['issue_codes']), (2, ['LIVE_SCHEDULE_STORED_CREDENTIALS_REQUIRED']))
        result, code = self.invoke(extra=['--init-validation-batch'])
        self.assertEqual((code, result['issue_codes']), (2, ['LIVE_SCHEDULE_INITIALIZATION_FORBIDDEN']))
        self.assertEqual(self.effects, [])

    def test_ordinary_and_scheduled_entries_cannot_reopen_historical_one_request_approval(self):
        with patch('monitor.manual_model_budget.MAX_REQUESTS', 1):
            historical = ManualModelBudget(self.directory / 'original-one-request-budget.json')
            historical.initialize(historical_amount=Decimal('0.001676'), historical_receipt_sha256='a' * 64)
            before = historical.path.read_bytes()
            for scheduled, source in ((False, 'env'), (False, 'prompt'), (False, 'stored'), (True, 'stored')):
                result, code = self.invoke(scheduled=scheduled, source=source, model_budget_factory=lambda: historical)
                self.assertEqual((code, result['issue_codes']), (2, ['MODEL_BUDGET_REQUEST_LIMIT_REACHED']))
                self.assertEqual(historical.path.read_bytes(), before)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.boundary_proofs, [])

    def test_usd_or_more_than_one_cny_cannot_enlarge_shared_approval(self):
        for currency, amount in (('USD', '0.20'), ('CNY', '1.01')):
            self.raw['deepseek'].update(currency=currency, amount=amount)
            self.write_inputs()
            result, code = self.invoke()
            self.assertEqual((code, result['issue_codes']), (2, ['LIVE_CNY_BUDGET_REQUIRED']))
        self.assertEqual(self.effects, [])
        self.assertEqual(self.boundary_proofs, [])

    def test_budget_consumed_after_preflight_still_blocks_model_wire_without_refund(self):
        def credentials(_source):
            for _ in range(9):
                self.shared.reserve(Decimal('0.001'), request_sha256='a' * 64)
            return ProjectCredentials(apify_token=SYNTHETIC_SECRET, deepseek_api_key=SYNTHETIC_SECRET,
                                      smtp_user=self.raw['sender'], smtp_password=SYNTHETIC_SECRET)
        self.queue_round([101], ['PRE_RESET'])
        result, code = self.invoke(credential_reader=credentials)
        self.assertEqual((code, result['classified'], result['sent']), (1, 0, 0))
        self.assertEqual((len(self.model.calls), len(self.smtp.calls)), (0, 0))
        self.assertEqual(self.batch.validate_ready()['counts']['deepseek_request'], 1)
        self.assertEqual(self.shared.snapshot()['requests'], 9)
        self.assertEqual(Decimal(self.shared.snapshot()['committed_cny']), Decimal('0.009'))

    def test_production_proof_catalogs_still_refuse_before_shared_budget_or_credentials(self):
        def forbidden():
            self.fail('Unreviewed production capabilities reached the budget')
        result, code = self.invoke(source_catalog=None, token_proofs=None, apify_cost_proofs=None,
                                   model_budget_factory=forbidden)
        self.assertEqual((code, result['status']), (2, 'REFUSED'))
        for issue in ('APIFY_SOURCE_CONTRACT_UNVERIFIED', 'DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE',
                      'APIFY_TOTAL_COST_BOUND_UNVERIFIED'):
            self.assertIn(issue, result['issue_codes'])
        self.assertEqual(self.effects, [])


if __name__ == '__main__':
    unittest.main()
