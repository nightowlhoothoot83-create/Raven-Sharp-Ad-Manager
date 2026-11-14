import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from publication import submit_once, ReconciliationRequired, publication_id

class Duplicate(Exception):
    code = 11000

class Records:
    def __init__(self): self.rows = {}
    async def insert_one(self, row):
        if row['_id'] in self.rows: raise Duplicate()
        self.rows[row['_id']] = dict(row)
    async def find_one(self, query, projection):
        row = self.rows.get(query['_id'])
        return dict(row) if row else None
    async def update_one(self, query, change): self.rows[query['_id']].update(change['$set'])

class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_requests_send_only_once_and_restart_reuses_receipt(self):
        records = Records(); calls = []
        async def send():
            calls.append(1); await asyncio.sleep(.01); return {'id': 'real-post'}
        args = dict(user_id='u', campaign_id='c', platform='instagram', account_id='a', content={'text':'x'}, send=send)
        results = await asyncio.gather(submit_once(records, **args), submit_once(records, **args), return_exceptions=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(isinstance(x, ReconciliationRequired) for x in results))
        repeated = await submit_once(records, **args)
        self.assertTrue(repeated['duplicate_prevented']); self.assertEqual(len(calls), 1)
    async def test_timeout_never_blindly_retries(self):
        records = Records(); calls = []
        async def send(): calls.append(1); raise TimeoutError('secret-token')
        args = dict(user_id='u', campaign_id='c', platform='facebook_page', account_id='a', content={}, send=send)
        for _ in range(2):
            with self.assertRaises(ReconciliationRequired): await submit_once(records, **args)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('secret-token', str(records.rows))
    async def test_tiktok_acceptance_is_only_submitted(self):
        async def send(): return {'data': {'publish_id': 'pending'}}
        result = await submit_once(Records(), user_id='u', campaign_id='c', platform='tiktok', account_id='a', content={}, send=send)
        self.assertEqual(result['status'], 'submitted')
    async def test_missing_receipt_enters_reconciliation(self):
        records = Records()
        async def send(): return {'success': True}
        with self.assertRaises(ReconciliationRequired):
            await submit_once(records, user_id='u', campaign_id='c', platform='pinterest', account_id='a', content={}, send=send)
        self.assertEqual(next(iter(records.rows.values()))['status'], 'needs_reconciliation')
    def test_identity_separates_accounts_users_and_content(self):
        base = ['u','c','p','a',{'text':'x'}]
        for index in range(4):
            changed = list(base); changed[index] += '2'
            self.assertNotEqual(publication_id(*base), publication_id(*changed))
        self.assertNotEqual(publication_id(*base), publication_id('u','c','p','a',{'text':'y'}))

if __name__ == '__main__': unittest.main()
