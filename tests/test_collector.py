import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ['COLLECTOR_DATA'] = cls.temp.name
        cls.module = importlib.import_module('collector.app')
        cls.client = cls.module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.module.db() as c:
            for table in ['release_assessments', 'review_checks', 'job_apps', 'discovery_checks', 'discovery_apps', 'apps', 'snapshots', 'reviews', 'edges', 'jobs', 'events']:
                c.execute(f'DELETE FROM {table}')

    def test_rejects_invalid_and_cross_origin_jobs(self):
        self.assertEqual(self.client.post('/api/jobs', json={'seeds': []}).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json={'seeds': ['slots'], 'max_apps': 1001}).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json={'seeds': ['slots']}, headers={'Origin': 'https://other.example'}).status_code, 403)

    def test_expanded_collection_limits_are_accepted_and_bounded(self):
        with patch.object(self.module.threading,'Thread'):
            response=self.client.post('/api/jobs',json={'seeds':['win slots'],'max_apps':1000,'review_limit':10000})
        self.assertEqual(response.status_code,202)
        with self.module.db() as c:
            cfg=json.loads(c.execute('SELECT config FROM jobs').fetchone()[0])
        self.assertEqual(cfg['max_apps'],1000)
        self.assertEqual(cfg['review_limit'],10000)
        self.assertEqual(self.client.post('/api/jobs',json={'seeds':['slots'],'review_limit':10001}).status_code,400)

    def test_low_review_pages_are_paced_and_deduplicated(self):
        from types import SimpleNamespace
        cfg=dict(country='in',language='en')
        first=[dict(reviewId=str(i),score=1) for i in range(200)]
        second=[dict(reviewId=str(i),score=1) for i in range(199,205)]
        with patch.object(self.module,'reviews',side_effect=[(first,SimpleNamespace(token='next')),(second,None)]) as fetch,patch.object(self.module.time,'sleep') as sleep:
            items,partial=self.module.low_review_pages('none','com.example.app',cfg,1,205)
        self.assertFalse(partial)
        self.assertEqual(len(items),205)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(fetch.call_args_list[0].kwargs['count'],200)
        self.assertIsNotNone(fetch.call_args_list[1].kwargs['continuation_token'])
        sleep.assert_called_once_with(1)

    def test_manual_testing_evidence_requires_source_and_is_visible(self):
        with self.module.db() as c:
            c.execute('INSERT INTO apps VALUES(?,?,?)',('com.example.beta',json.dumps(dict(appId='com.example.beta',version='1.0')),self.module.now()))
        initial=self.client.get('/api/apps/com.example.beta').get_json()
        self.assertEqual(initial['metadata']['release']['status'],'possible_beta')
        self.assertEqual(self.client.post('/api/apps/com.example.beta/release-status',json={'status':'confirmed_beta'}).status_code,400)
        response=self.client.post('/api/apps/com.example.beta/release-status',json={'status':'confirmed_beta','note':'Verified the beta label in the tester listing.','source_url':'https://play.google.com/store/apps/details?id=com.example.beta','observed_version':'1.0'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get('/api/state').get_json()['apps'][0]['release']['status'],'confirmed_beta')
        self.assertEqual(self.client.post('/api/apps/com.example.beta/release-status',json={'status':'unknown'}).status_code,200)
        self.assertEqual(self.client.get('/api/state').get_json()['apps'][0]['release']['status'],'possible_beta')

    def test_collection_deduplicates_and_preserves_evidence(self):
        cfg = dict(seeds=['slots', 'https://play.google.com/store/apps/details?id=com.example.game'], country='in', language='en', results_per_keyword=10, max_apps=5, review_limit=5, depth=0, same_developer=False, related=False)
        metadata = {'appId': 'com.example.game', 'title': 'Example', 'developer': 'Developer', 'genre': 'Casino', 'reviews': 1234, 'ratings': 5678, 'developerId': 'dev-1'}
        with self.module.db() as c:
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', ('test', 'queued', json.dumps(cfg), self.module.now()))
        with patch.object(self.module, 'search', return_value=[{'appId': 'com.example.game'}]), patch.object(self.module, 'play_app', return_value=metadata), patch.object(self.module, 'reviews', return_value=([{'reviewId': 'r1', 'content': 'Review'}], None)), patch.object(self.module.time, 'sleep'):
            self.module.run_job('test', cfg)
        state = self.client.get('/api/state').get_json()
        self.assertEqual(len(state['apps']), 1)
        self.assertEqual(state['review_count'], 1)
        self.assertEqual(state['apps'][0]['total_reviews'], 1234)
        self.assertEqual(state['apps'][0]['total_ratings'], 5678)
        self.assertEqual(state['apps'][0]['saved_reviews'], 1)
        self.assertEqual(state['jobs'][0]['status'], 'completed')
        detail = self.client.get('/api/apps/com.example.game').get_json()
        self.assertEqual(len(detail['sources']), 2)
        self.assertEqual(detail['metadata']['play_url'], 'https://play.google.com/store/apps/details?id=com.example.game')
        self.assertEqual(detail['review_count'], 1)
        exported = self.client.get('/api/export').get_json()
        self.assertIn('snapshots', exported)
        self.assertEqual(exported['apps'][0]['total_reviews'], 1234)
        self.assertIn('total_reviews,total_ratings,saved_reviews', self.client.get('/api/export?format=csv').get_data(as_text=True))

    def test_cached_apps_still_expand_discovery_without_rescraping(self):
        cfg = dict(seeds=['com.example.seed'], country='in', language='en', max_apps=10,
                   review_limit=5, depth=1, same_developer=True, related=True)
        cached = dict(appId='com.example.seed', title='Saved', country='in', language='en')
        with self.module.db() as c:
            c.execute('INSERT INTO apps VALUES(?,?,?)', ('com.example.seed', json.dumps(cached), self.module.now()))
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', ('reuse', 'queued', json.dumps(cfg), self.module.now()))
        page = '<section><h2>More by Developer</h2><a href="/store/apps/details?id=com.example.next">Next</a></section><section><h2>Similar games</h2><a href="/store/apps/details?id=com.example.next">Next</a></section>'
        response = unittest.mock.Mock(text=page)
        with patch.object(self.module, 'play_app', return_value={'appId': 'com.example.next', 'title': 'Next'}) as fetch, patch.object(self.module, 'reviews', return_value=([], None)) as fetch_reviews, patch.object(self.module.requests, 'get', return_value=response), patch.object(self.module.time, 'sleep'):
            self.module.run_job('reuse', cfg)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], 'com.example.next')
        self.assertEqual(fetch_reviews.call_count, 1)
        state = self.client.get('/api/state').get_json()
        self.assertEqual(state['jobs'][0]['reused'], 1)
        self.assertEqual(state['jobs'][0]['collected'], 1)
        tracked = {item['package']: item for item in state['jobs'][0]['apps']}
        self.assertEqual(tracked['com.example.seed']['acquisition'], 'reused')
        self.assertEqual(tracked['com.example.next']['acquisition'], 'new')
        self.assertEqual(tracked['com.example.next']['root_app'], 'com.example.seed')
        self.assertEqual(tracked['com.example.next']['root_seed'], 'com.example.seed')
        self.assertEqual(tracked['com.example.next']['source'], 'com.example.seed')
        self.assertEqual(tracked['com.example.next']['depth'], 1)
        self.assertEqual(tracked['com.example.next']['status'], 'completed')
        detail = self.client.get('/api/apps/com.example.next').get_json()
        self.assertEqual({s['relation'] for s in detail['sources']}, {'same_developer', 'similar_games'})
        seed_detail = self.client.get('/api/apps/com.example.seed').get_json()
        self.assertEqual(seed_detail['connections']['same_developer'][0]['package_id'], 'com.example.next')
        self.assertTrue(seed_detail['connections']['similar_games'][0]['collected'])
        self.assertEqual(seed_detail['discovery_status']['similar_games']['status'], 'checked')
        self.assertEqual(detail['discovery_status']['same_developer']['status'], 'not_explored')

    def test_see_more_expansion_preserves_uncollected_candidates(self):
        cfg = dict(seeds=['com.example.seed'], country='in', language='en', max_apps=1,
                   review_limit=0, depth=1, same_developer=False, related=True)
        cached = dict(appId='com.example.seed', title='Saved', country='in', language='en')
        with self.module.db() as c:
            c.execute('INSERT INTO apps VALUES(?,?,?)', ('com.example.seed', json.dumps(cached), self.module.now()))
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', ('expand', 'queued', json.dumps(cfg), self.module.now()))
        page = '<section><h2>Similar games</h2><a href="/store/apps/collection/cluster?gsr=example">See more</a><a href="/store/apps/details?id=com.visible.app">Visible</a></section>'
        response = unittest.mock.Mock(text=page)
        with patch.object(self.module.requests, 'get', return_value=response), patch.object(self.module, 'expanded_apps', return_value={'apps': [{'appId': 'com.more.app', 'title': 'Expanded App'}], 'limit_reached': True}) as expand, patch.object(self.module.time, 'sleep'):
            self.module.run_job('expand', cfg)
        expand.assert_called_once_with('/store/apps/collection/cluster?gsr=example', 'in', 'en', 1)
        detail = self.client.get('/api/apps/com.example.seed').get_json()
        matches = {item['package_id']: item for item in detail['connections']['similar_games']}
        self.assertFalse(matches['com.more.app']['collected'])
        self.assertEqual(matches['com.more.app']['title'], 'Expanded App')
        self.assertIn('com.visible.app', matches)

    def test_section_parser_excludes_other_links(self):
        from collector.discovery import parse_relationships
        html = '<a href="/store/apps/details?id=com.other.app">Outside</a><section><h2>More by Studio</h2><a href="/store/apps/details?id=com.studio.app&amp;hl=en">App</a></section><section><h2>Similar games</h2><a href="/store/apps/details?id=com.similar.app">App</a><a href="/store/apps/details?id=com.similar.app">Duplicate</a></section><section><h2>Ads</h2><a href="/store/apps/details?id=com.ad.app">Ad</a></section>'
        matches, found = parse_relationships(html)
        self.assertEqual(matches, {'same_developer': ['com.studio.app'], 'similar_games': ['com.similar.app']})
        self.assertEqual(found, {'same_developer', 'similar_games'})

    def test_targeted_low_star_collection_is_separate_and_deduplicated(self):
        cfg = dict(seeds=['com.example.low'], country='in', language='en', max_apps=1,
                   review_limit=3, low_star_limit=2, depth=0)
        with self.module.db() as c:
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', ('low', 'queued', json.dumps(cfg), self.module.now()))
        def fetch(package, **kwargs):
            rating=kwargs.get('filter_score_with', 5)
            return ([dict(reviewId=str(rating),content='Review',score=rating)],None)
        with patch.object(self.module,'play_app',return_value={'appId':'com.example.low'}), patch.object(self.module,'reviews',side_effect=fetch) as reviews, patch.object(self.module.time,'sleep'):
            self.module.run_job('low',cfg)
        self.assertEqual(reviews.call_count,3)
        state=self.client.get('/api/state').get_json()
        self.assertEqual(state['apps'][0]['saved_reviews'],3)
        self.assertEqual(state['apps'][0]['low_star_reviews'],2)
        self.assertEqual([call.kwargs.get('filter_score_with') for call in reviews.call_args_list],[None,1,2])

    def test_low_only_mode_keeps_missing_and_failed_evidence_distinct(self):
        cfg=dict(seeds=['com.example.lowonly'],country='in',language='en',max_apps=1,review_mode='low_only',review_limit=2,depth=0)
        with self.module.db() as c:
            c.execute('INSERT INTO apps VALUES(?,?,?)',('com.example.lowonly',json.dumps(dict(appId='com.example.lowonly',country='in',language='en')),self.module.now()))
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)',('lowonly','queued',json.dumps(cfg),self.module.now()))
        def fetch(package,**kwargs):
            star=kwargs['filter_score_with']
            if star==2:
                raise RuntimeError('Network unavailable')
            return ([] if star==1 else [dict(reviewId='three',content='Complaint',score=3),dict(reviewId='five',content='Praise',score=5)],None)
        with patch.object(self.module,'play_app') as metadata,patch.object(self.module,'reviews',side_effect=fetch),patch.object(self.module.time,'sleep'):
            self.module.run_job('lowonly',cfg)
        metadata.assert_not_called()
        detail=self.client.get('/api/apps/com.example.lowonly').get_json()
        self.assertEqual([r['score'] for r in detail['reviews']],[3])
        self.assertEqual({r['rating']:r['status'] for r in detail['review_checks']},{1:'empty_returned',2:'failed',3:'available'})
        self.assertEqual(detail['low_star_count'],1)

    def test_failures_are_not_successes(self):
        cfg = dict(seeds=['com.example.missing'], country='in', language='en', max_apps=1, review_limit=0, depth=0)
        with self.module.db() as c:
            c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', ('failure', 'queued', json.dumps(cfg), self.module.now()))
        with patch.object(self.module, 'play_app', side_effect=RuntimeError('Unavailable')), patch.object(self.module.time, 'sleep'):
            self.module.run_job('failure', cfg)
        state = self.client.get('/api/state').get_json()
        self.assertEqual(state['jobs'][0]['status'], 'completed_with_warnings')
        self.assertEqual(state['apps'], [])
        self.assertEqual(state['events'][1]['level'], 'error')


if __name__ == '__main__':
    unittest.main()
