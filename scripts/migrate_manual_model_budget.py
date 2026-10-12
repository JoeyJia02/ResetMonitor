"""Explicit, idempotent import of the one completed model usage record.

Does not read credentials, call a provider, reset an existing ledger, or send.
"""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.manual_model_budget import ManualModelBudget, ModelBudgetError

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / 'runtime/recovered-full-chain-20261011/full-chain-20261011-01.receipt.json'
RECEIPT_SHA256 = '55e568414fe4d02ce139a43d52c6c90e50fcb7c1ad3c7066a4484d47fe4bb2e9'

def main():
    if sys.argv[1:] != ['--import-known-usage']:
        print('PREVIEW_ONLY: imports one historical request, CNY0.001676; no new IO.')
        return 0
    raw = RECEIPT.read_bytes()
    if hashlib.sha256(raw).hexdigest() != RECEIPT_SHA256:
        raise ValueError()
    data = json.loads(raw)
    usage = data['analysis']['usage']
    if (data.get('model_attempts') != 1 or data.get('analyzed_items') != 3
            or usage != {'prompt_tokens':470,'completion_tokens':92,'total_tokens':562}
            or data['analysis']['usage_cost_upper_bound_cny'] != '0.001676'):
        raise ValueError()
    ledger = ManualModelBudget()
    snapshot = ledger.initialize(historical_amount=Decimal('0.001676'), historical_receipt_sha256=RECEIPT_SHA256)
    if (snapshot['requests'] != 1 or snapshot['committed_cny'] != '0.001676'
            or snapshot['entries'][0]['reference_sha256'] != RECEIPT_SHA256):
        raise ValueError()
    result = {'schema_version':1,'status':'HISTORICAL_USAGE_IMPORTED',
        'ledger_path':str(ledger.path),'currency':'CNY','approved_limit':'1.00',
        'maximum_model_requests':1,'historical_model_requests':1,
        'committed_usage_cost_upper_bound_cny':'0.001676',
        'historical_receipt_sha256':RECEIPT_SHA256,'amount_is_invoice':False,
        'new_source_runs':0,'new_model_requests':0,'new_smtp_attempts':0}
    (ROOT / 'evidence/manual-model-budget-migration-20261011.json').write_text(
        json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except ModelBudgetError as error:
        print(error.code)
        sys.exit(1)
    except Exception:
        print('MODEL_BUDGET_MIGRATION_RECONCILIATION_REQUIRED')
        sys.exit(1)
