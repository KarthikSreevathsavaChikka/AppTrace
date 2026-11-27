import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from openpyxl import load_workbook
from collector.excel_export import build_workbook


class ExcelExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'export.sqlite'
        def db():
            c=sqlite3.connect(self.path)
            c.row_factory=sqlite3.Row
            return c
        self.db=db
        with db() as c:
            c.executescript('''
            CREATE TABLE apps(package TEXT,metadata TEXT,collected TEXT);
            CREATE TABLE reviews(package TEXT,data TEXT);
            CREATE TABLE apk_downloads(id TEXT,batch TEXT,package TEXT,architecture TEXT,status TEXT,version_code INTEGER,version_name TEXT,error TEXT,created TEXT,finished TEXT,attempts INTEGER,existing_download_id TEXT,cancelled_by TEXT);
            CREATE TABLE apk_files(id TEXT,download_id TEXT,relative_path TEXT,kind TEXT,split_name TEXT,size_bytes INTEGER,sha256 TEXT);
            ''')
            c.execute('INSERT INTO apps VALUES(?,?,?)',('com.example.game',json.dumps({'title':'=untrusted title','developer':'Studio','reviews':100,'ratings':200}),'2026-11-27'))
            c.execute('INSERT INTO apps VALUES(?,?,?)',('com.example.other','{"title":"Not downloaded"}','2026-11-27'))
            for identifier,status,date,error,reference in [('ok','downloaded','1',None,None),('bad','failed','3','HTTP 404',None),('reuse','skipped','2',None,'ok')]:
                c.execute('INSERT INTO apk_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,'batch','com.example.game','arm64',status,123,'1.2.3',error,date,date,1,reference,None))
            c.execute('INSERT INTO apk_files VALUES(?,?,?,?,?,?,?)',('base','ok','apks/com.example.game/123/arm64/run/base.apk','base',None,1024,'a'*64))
            c.execute('INSERT INTO apk_files VALUES(?,?,?,?,?,?,?)',('split','ok','apks/com.example.game/123/arm64/run/split-001.apk','split','config.en',2048,'b'*64))

    def tearDown(self):
        self.temp.cleanup()

    def test_export_retains_failures_references_and_formula_safety(self):
        workbook=load_workbook(build_workbook(self.db))
        self.assertEqual(workbook.sheetnames,['Read me','Apps','APK downloads','APK files'])
        apps=workbook['Apps']
        self.assertEqual(apps['A2'].value,'=untrusted title')
        self.assertEqual(apps['A2'].data_type,'s')
        self.assertEqual(apps['O2'].value,'failed')
        self.assertEqual(apps['R2'].value,'HTTP 404')
        self.assertEqual(apps['S2'].value,2)
        self.assertEqual(apps['O3'].value,'not requested')
        downloads=workbook['APK downloads']
        self.assertEqual(downloads.max_row,4)
        statuses={row[5].value for row in list(downloads.rows)[1:]}
        self.assertEqual(statuses,{'downloaded','failed','skipped'})
        files=workbook['APK files']
        self.assertEqual(files.max_row,3)
        self.assertEqual(files['H2'].value,'base.apk')
        self.assertEqual(files['K2'].hyperlink.target,'./apks/com.example.game/123/arm64/run/base.apk')
        self.assertEqual(files['N2'].value,'a'*64)
        self.assertEqual(files.freeze_panes,'A2')
        self.assertTrue(files.auto_filter.ref)
