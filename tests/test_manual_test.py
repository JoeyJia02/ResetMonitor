"""Actual manual entry point, synthetic credentials and fake wire only."""
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from monitor.credentials import ProjectCredentials
from scripts import manual_test as probe
from tests.test_classification_setup_probe import FakeResponse, FakeTransport
from tests.test_smtp_transport import FakeFactory, FakeSession

ROOT = Path(os.environ.get('RESET_MONITOR_TEST_ROOT',
                str(Path(__file__).resolve().parents[1] / 'runtime/test-tmp'))).resolve()


class ManualTestEntryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('socket.socket', 'socket.create_connection', 'smtplib.SMTP', 'smtplib.SMTP_SSL'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('REAL_IO_FORBIDDEN')))
        self.directory = ROOT / ('mt-' + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.output = io.StringIO()
        self.credentials = ProjectCredentials(apify_token='SYNTHETIC_APIFY_SECRET',
            deepseek_api_key='SYNTHETIC_DEEPSEEK_SECRET', smtp_user=probe.SENDER,
            smtp_password='abcdefghijklmnop')
        self.now = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        self.tweets = [{'id':str(2109152716954222850+i), 'created_at':'2026-10-15T07:00:00Z',
            'text':'完整测试原文'+str(i), 'url':'https://x.com/thsottiaux/status/'+str(2109152716954222850+i),
            'context':{'quoted_tweet':{'text':'完整引用原文'}}} for i in range(3)]
        document = {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({
            'results':[{'id':str(i),'label':'IRRELEVANT','reset_time':None,'reason':'未确认；时间不明。'} for i in range(1,4)]})}}],
            'usage':{'prompt_tokens':470,'completion_tokens':92,'total_tokens':562}}
        self.transport = FakeTransport(FakeResponse(document))
        self.factory = FakeFactory()
        self.source_calls = 0
        self.credential_reads = 0

    def cleanup(self):
        if not self.directory.resolve().is_relative_to(ROOT) or self.directory.resolve() == ROOT:
            raise AssertionError('UNSAFE_TEST_CLEANUP')
        shutil.rmtree(self.directory)

    def source(self, *_):
        self.source_calls += 1
        return self.tweets, {'source_posts':1, 'run_charge_cap_usd':'0.0009'}

    def credentials_loader(self):
        self.credential_reads += 1
        return self.credentials

    def run_manual(self, run_id, **overrides):
        options = dict(output=self.output, directory=self.directory,
            clock=lambda:self.now, transport=self.transport, source_fetch=self.source,
            smtp_factory=self.factory, credential_loader=self.credentials_loader)
        options.update(overrides)
        return probe.main(['--run','--run-id',run_id,'--confirmed'], **options)

    def test_manual_entry_runs_after_old_expiry_and_can_run_a_second_explicit_round(self):
        with patch('monitor.manual_model_budget.ManualModelBudget._load',
                   side_effect=AssertionError('OLD_BUDGET_MUST_NOT_BLOCK_MANUAL_TEST')):
            for _ in range(2):
                run_id = 'manual-test-' + uuid4().hex
                self.assertEqual(self.run_manual(run_id), 0)
                result = json.loads(probe._path(self.directory,'result',run_id).read_text('utf-8'))
                self.assertEqual(len(result['results']), 3)
                self.assertEqual(result['notification']['message_id'], '<'+run_id+'@qq.com>')
        self.assertEqual((self.source_calls,len(self.transport.calls),len(self.factory.calls)), (2,2,2))
        for request, _ in self.transport.calls:
            payload=json.loads(json.loads(request.data)['messages'][1]['content'])
            self.assertEqual(len(payload['tweets']),3)
            self.assertEqual(payload['tweets'][0]['quoted_tweet']['text'],'完整引用原文')

    def test_same_run_is_not_repeated_and_does_not_reread_credentials(self):
        run_id = 'manual-test-' + uuid4().hex
        self.assertEqual(self.run_manual(run_id),0)
        before={path.name:path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(self.run_manual(run_id),2)
        self.assertEqual((self.credential_reads,self.source_calls,len(self.transport.calls),len(self.factory.calls)),(1,1,1,1))
        self.assertEqual(before,{path.name:path.read_bytes() for path in self.directory.iterdir()})

    def test_preview_and_missing_confirmation_do_not_load_credentials_or_call_services(self):
        for argv in ([],['--preview'],['--help']):
            self.assertEqual(probe.main(argv, output=self.output,
                credential_loader=lambda:self.fail('NO_CREDENTIAL_READ')),0)
        self.assertEqual(probe.main(['--run','--run-id','manual-test-'+uuid4().hex],output=self.output,
            credential_loader=lambda:self.fail('NO_CREDENTIAL_READ')),2)
        self.assertEqual((self.source_calls,len(self.transport.calls),len(self.factory.calls)),(0,0,0))

    def test_failed_source_stays_failed_without_model_mail_or_automatic_retry(self):
        def fail(*_):
            self.source_calls += 1
            raise probe.OneShotSourceError('ONE_SHOT_SOURCE_ROW_FAILED')
        run_id='manual-test-'+uuid4().hex
        self.assertEqual(self.run_manual(run_id,source_fetch=fail),1)
        self.assertEqual(self.run_manual(run_id),2)
        self.assertEqual((self.source_calls,len(self.transport.calls),len(self.factory.calls)),(1,0,0))

    def test_smtp_unknown_preserves_analysis_and_does_not_resend(self):
        run_id='manual-test-'+uuid4().hex
        factory=FakeFactory(FakeSession(send=OSError('SYNTHETIC_TLS_FAILURE')))
        self.assertEqual(self.run_manual(run_id,smtp_factory=factory),1)
        self.assertTrue(probe._path(self.directory,'result',run_id).exists())
        self.assertEqual(self.run_manual(run_id,smtp_factory=factory),2)
        self.assertEqual((self.source_calls,len(self.transport.calls),len(factory.calls)),(1,1,1))

    def test_four_class_results_and_reset_time_reach_one_summary_mail(self):
        rows = [{'id': str(i), 'label': label, 'reset_time': timestamp, 'reason': reason}
            for i, label, timestamp, reason in (
                (1, 'POST_RESET', None, '主文"All propagated"表示已生效，时间不明。'),
                (2, 'PRE_RESET', '2026-10-15T20:00:00+08:00', '主文"will reset"预告20:00 UTC+8额外重置。'),
                (3, 'POSSIBLE_RESET', None, '证据不足，时间不明。'))]
        document = {'choices': [{'finish_reason':'stop', 'message': {'role':'assistant',
            'content':json.dumps({'results':rows})}}],
            'usage':{'prompt_tokens':1500,'completion_tokens':600,'total_tokens':2100}}
        self.transport = FakeTransport(FakeResponse(document))
        run_id='manual-test-'+uuid4().hex
        self.assertEqual(self.run_manual(run_id),0)
        result=json.loads(probe._path(self.directory,'result',run_id).read_text('utf-8'))
        for expected, actual in zip(rows, result['results']):
            self.assertEqual(actual['label'],expected['label'])
            self.assertEqual(actual['reset_time'],expected['reset_time'])
            self.assertEqual(set(actual),{'id','label','reset_time','reason'})
            self.assertIn(expected['label'],result['notification']['body'])
        self.assertIn('2026-10-15T20:00:00+08:00',result['notification']['body'])
        self.assertEqual((self.source_calls,len(self.transport.calls),len(self.factory.calls)),(1,1,1))


if __name__ == '__main__':
    unittest.main()
