import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from campaign_import import plan_import, import_draft, public_https
from test_publication import Records

class ImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manifest = json.loads((Path(__file__).parent / 'fixtures/content-creator-campaign.json').read_text())
        self.args = dict(user_id='u', brand_id='b', manifest=self.manifest,
            media_urls={'slide-01.png': 'https://cdn.mycalctools.net/slide-01.png'}, board_ids={'pinterest': 'board1'})

    def test_actual_creator_manifest_maps_and_preserves_content(self):
        plan = plan_import(**self.args)
        self.assertFalse(plan['published'])
        self.assertEqual(plan['status'], 'dry_run')
        self.assertEqual([p['platform'] for p in plan['posts']], ['pinterest','facebook_page','instagram','youtube','tiktok'])
        self.assertEqual([p['status'] for p in plan['posts']], ['prepared_for_review'] * 3 + ['blocked'] * 2)
        self.assertEqual(plan['posts'][1]['content']['text'], self.manifest['posts'][1]['caption'])
        self.assertEqual(plan['posts'][1]['content']['link'], self.manifest['posts'][1]['destination'])

    def test_missing_media_and_board_are_blocked(self):
        args = {**self.args, 'media_urls': {}, 'board_ids': {}}
        plan = plan_import(**args)
        self.assertTrue(all('public_https_media_url_required' in p['blockers'] for p in plan['posts']))
        self.assertIn('pinterest_board_id_required', plan['posts'][0]['blockers'])

    def test_video_is_explicitly_blocked(self):
        args = {**self.args, 'media_urls': {'short.mp4': 'https://cdn.mycalctools.net/short.mp4'},
            'selections': {'facebook_page': 'short.mp4', 'youtube': 'short.mp4'}}
        plan = plan_import(**args)
        for p in (plan['posts'][1], plan['posts'][3]):
            self.assertIn('unsupported_media_type_video_or_other', p['blockers'])
            self.assertIsNone(p['content']['image_url'])

    async def test_duplicate_import_is_atomic_and_keeps_draft(self):
        records = Records()
        first = await import_draft(records, **self.args)
        second = await import_draft(records, **self.args)
        self.assertEqual(first['campaign_id'], second['campaign_id'])
        self.assertTrue(second['duplicate_prevented'])
        self.assertEqual(len(records.rows), 1)
        self.assertEqual(next(iter(records.rows.values()))['status'], 'Draft')
        self.assertNotEqual(plan_import(**{**self.args, 'user_id': 'different'})['campaign_id'], first['campaign_id'])

    def test_invalid_network_urls_are_blocked(self):
        for url in ['http://cdn.mycalctools.net/a.png','https://127.0.0.1/a','https://10.0.0.1/a',
            'https://169.254.169.254/a','https://localhost/a','https://foo.local/a','https://[::1]/a',
            'https://user:pass@cdn.mycalctools.net/a','https://cdn.mycalctools.net:8443/a', 'https://example.com/a']:
            with self.subTest(url=url):
                self.assertFalse(public_https(url))
                plan = plan_import(**{**self.args,'media_urls': {'slide-01.png': url}})
                self.assertIn('public_https_media_url_required', plan['posts'][0]['blockers'])

    def test_affiliate_disclosure_and_signed_link_preserved(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['affiliate'] = True
        link = 'https://merchant.mycalctools.net/?signature=a%2Bb&affiliate=42'
        for p in manifest['posts']:
            p['destination'] = link
            p['caption'] = 'Ad / affiliate link: I may earn a commission if you buy. ' + p['caption']
        plan = plan_import(**{**self.args,'manifest': manifest})
        self.assertEqual(plan['posts'][0]['content']['link'], link)
        self.assertEqual(plan['posts'][0]['content']['text'], manifest['posts'][0]['caption'])
        manifest['posts'][0]['caption'] = 'Buy now'
        self.assertIn('affiliate_disclosure_required', plan_import(**{**self.args,'manifest': manifest})['posts'][0]['blockers'])

    def test_duplicate_platform_is_invalid(self):
        self.manifest['posts'].append(copy.deepcopy(self.manifest['posts'][0]))
        with self.assertRaises(ValueError): plan_import(**self.args)

if __name__ == '__main__': unittest.main()
