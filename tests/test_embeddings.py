import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from flask import Flask
from collector import embeddings
from collector.review_intelligence import install


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'test.sqlite'
        def db():
            c=sqlite3.connect(self.path)
            c.row_factory=sqlite3.Row
            return c
        self.db=db
        with db() as c:
            c.executescript('CREATE TABLE reviews(package TEXT,country TEXT,language TEXT,review_id TEXT,data TEXT); CREATE TABLE apps(package TEXT,metadata TEXT);')
            c.execute('INSERT INTO apps VALUES(?,?)',('com.example.app',json.dumps({'title':'Example'})))
        self.app=Flask(__name__)
        self.worker=install(self.app,db)
        self.client=self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def review(self, review_id, text, score):
        with self.db() as c:
            c.execute('INSERT INTO reviews VALUES(?,?,?,?,?)',('com.example.app','in','en',review_id,json.dumps({'reviewId':review_id,'content':text,'score':score})))

    def run_index(self, job):
        with self.db() as c:
            c.execute('INSERT INTO embedding_jobs(id,status) VALUES(?,?)',(job,'queued'))
        self.worker(job,None)

    @staticmethod
    def fake_encode(texts,query=False):
        values=np.zeros((len(texts),768),dtype='<f4')
        for i,text in enumerate(texts):
            values[i,0 if 'money' in text.lower() else 1]=1
        return values

    def test_cache_priority_search_filters_and_changed_review(self):
        self.review('positive','Great game',5)
        self.review('low','money never arrived',1)
        self.review('duplicate','money never arrived',2)
        self.review('middle','ads complaint',3)
        with patch.object(embeddings,'encode',side_effect=self.fake_encode) as encode:
            self.run_index('first')
            self.assertEqual(encode.call_args.args[0][0],'money never arrived')
            self.run_index('second')
            self.assertEqual(encode.call_count,1)
            status=self.client.get('/api/intelligence').get_json()
            self.assertEqual(status['totals'],dict(total=3,indexed=3,low_star=3,low_star_indexed=3))
            with self.db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0],2)
            result=self.client.post('/api/intelligence/search',json={'query':'money problems','max_stars':2}).get_json()
            self.assertEqual(len(result['results']),2)
            self.assertTrue(all(r['review']['score']<=2 for r in result['results']))
            self.assertEqual(result['results'][0]['similarity'],1.0)
            with self.db() as c:
                c.execute('UPDATE reviews SET data=? WHERE review_id=?',(json.dumps({'reviewId':'low','content':'changed complaint','score':1}),'low'))
            self.assertEqual(self.client.get('/api/intelligence').get_json()['totals']['indexed'],2)
            result=self.client.post('/api/intelligence/search',json={'query':'money','max_stars':2}).get_json()
            self.assertEqual(len(result['results']),1)
            self.run_index('third')
            self.assertEqual(self.client.get('/api/intelligence').get_json()['totals']['indexed'],3)

    def test_all_results_option_exceeds_normal_limit(self):
        for i in range(125):
            self.review(str(i),'money not received',1)
        with patch.object(embeddings,'encode',side_effect=self.fake_encode):
            self.run_index('all')
            limited=self.client.post('/api/intelligence/search',json={'query':'money','limit':30}).get_json()
            unlimited=self.client.post('/api/intelligence/search',json={'query':'money','limit':'all'}).get_json()
        self.assertEqual(len(limited['results']),30)
        self.assertEqual(len(unlimited['results']),125)
        self.assertEqual(unlimited['indexed_candidates'],125)
        self.assertEqual(self.client.post('/api/intelligence/search',json={'query':'money','limit':'invalid'}).status_code,400)

    def test_model_revision_isolation(self):
        self.review('low','money missing',1)
        with patch.object(embeddings,'encode',side_effect=self.fake_encode):
            self.run_index('first')
        with patch.object(embeddings,'model_key',return_value='another-model-revision'):
            self.assertEqual(self.client.get('/api/intelligence').get_json()['totals']['indexed'],0)
            self.assertEqual(self.client.post('/api/intelligence/search',json={'query':'money'}).get_json()['results'],[])

    def test_failure_and_cancel_do_not_claim_indexed(self):
        self.review('low','money missing',1)
        with patch.object(embeddings,'encode',side_effect=RuntimeError('model unavailable')):
            self.run_index('failed')
        status=self.client.get('/api/intelligence').get_json()
        self.assertEqual(status['jobs'][0]['status'],'failed')
        self.assertEqual(status['totals']['indexed'],0)
        self.assertEqual(self.client.post('/api/intelligence/search',json={'query':''}).status_code,400)
        self.assertEqual(self.client.post('/api/intelligence/search',json={'query':'money','max_stars':8}).status_code,400)
        with self.db() as c:
            c.execute("INSERT INTO embedding_jobs(id,status) VALUES('active','running')")
        self.assertEqual(self.client.post('/api/intelligence/index',json={}).status_code,409)
        self.client.post('/api/intelligence/jobs/active/stop',json={})
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status FROM embedding_jobs WHERE id='active'").fetchone()[0],'stopping')
