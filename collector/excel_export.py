"""Shareable app inventory and original APK file index, without APK binaries."""
from datetime import datetime,timezone
import io
import json
from pathlib import PurePosixPath
from urllib.parse import urlencode
from collector.release_status import assess


def build_workbook(db):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment,Font,PatternFill
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter
    # A read transaction keeps app, attempt, and file tables at one SQLite snapshot.
    with db() as c:
        c.execute('BEGIN')
        apps=[dict(r) for r in c.execute('SELECT * FROM apps ORDER BY package')]
        downloads=[dict(r) for r in c.execute('SELECT * FROM apk_downloads ORDER BY created DESC,id')]
        files=[dict(r) for r in c.execute('SELECT * FROM apk_files ORDER BY download_id,kind,relative_path')]
        assessments={r['package']:dict(r) for r in c.execute('SELECT * FROM release_assessments')} if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_assessments'").fetchone() else {}
        review_counts={(r[0],r[1]):r[2] for r in c.execute("SELECT package,CAST(json_extract(data,'$.score') AS INTEGER),COUNT(*) FROM reviews GROUP BY package,CAST(json_extract(data,'$.score') AS INTEGER)")}
    metadata={a['package']:json.loads(a['metadata']) for a in apps}
    attempts={}
    file_sets={}
    for d in downloads:
        attempts.setdefault(d['package'],[]).append(d)
    for f in files:
        file_sets.setdefault(f['download_id'],[]).append(f)
    by_id={d['id']:d for d in downloads}
    workbook=Workbook()
    workbook.remove(workbook.active)

    def sheet(name,headers,rows):
        ws=workbook.create_sheet(name)
        ws.append(headers)
        for values in rows:
            ws.append(values)
        for row in ws:
            for cell in row:
                if isinstance(cell.value,str):
                    # Store untrusted titles/errors as text, never Excel formulas.
                    cell.value=ILLEGAL_CHARACTERS_RE.sub('',cell.value)[:32767]
                    cell.data_type='s'
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        for cell in ws[1]:
            cell.fill=PatternFill('solid',fgColor='246B48')
            cell.font=Font(color='FFFFFF',bold=True)
        ws.freeze_panes='A2'
        ws.auto_filter.ref=ws.dimensions
        ws.sheet_view.showGridLines=False
        for index,title in enumerate(headers,1):
            width=48 if any(word in title.lower() for word in ('path','error','sha-256','notes')) else 30 if any(word in title.lower() for word in ('name','package','developer','time','id')) else 22
            ws.column_dimensions[get_column_letter(index)].width=width
        for col,title in enumerate(headers,1):
            if 'status' in title.lower():
                colors={'downloaded':'E0F0E4','skipped':'E0F0E4','failed':'FBE5E1','blocked':'FFF1D6','paused':'FFF1D6','cancelled':'EDEDED','not requested':'EDEDED'}
                for row in range(2,ws.max_row+1):
                    cell=ws.cell(row,col)
                    if cell.value in colors:
                        cell.fill=PatternFill('solid',fgColor=colors[cell.value])
        return ws

    notes=[
        ['Exported at (UTC)',datetime.now(timezone.utc).isoformat()],
        ['Contents','Apps: one row per app; APK downloads: every attempt, including failures; APK files: one row per registered base/split file.'],
        ['Sharing','Place this Excel workbook beside collector.sqlite and the apks/ folder. Preserve the folder structure. The workbook contains references, not APK binaries.'],
        ['Relative paths','Paths start at the folder containing collector.sqlite. APK file links work when the workbook is placed in that same folder. Excel may ask permission to open local files.'],
        ['Download status','Failed, cancelled, queued, paused, and blocked attempts may have no files. See Error details. Not requested means no download attempt exists.'],
        ['Multiple attempts','The latest attempt can fail even if an older successful version exists. Apps lists registered-file totals and architectures separately.'],
        ['Skipped','Skipped means a verified local version was reused. Referenced download ID identifies the source attempt; APK files lists each registered file once.'],
        ['Snapshot','Statuses are a point-in-time database snapshot. Queued or downloading entries can change after export. Completed-file records do not guarantee you included the physical files when sharing.'],
        ['Validation','Downloaded files were checked for manifest package/version consistency and ZIP integrity. Hashes identify the original bytes; signing certificates and complete variant/asset-pack coverage were not verified.'],
        ['Review counts','Stored review records may cover multiple locales. 1-, 2-, and 3-star counts are saved written-review records, separate from Play Store rating/review totals.'],
        ['Credentials','Do not include Google auth files, tokens, or account credentials in the shared folder.'],
    ]
    intro=sheet('Read me',['Topic','Notes'],notes)
    intro.column_dimensions['A'].width=28
    intro.column_dimensions['B'].width=110
    for row in range(2,intro.max_row+1):
        intro.row_dimensions[row].height=60
    app_rows=[]
    for a in apps:
        m=metadata[a['package']]
        release=assess(dict(m,package_id=a['package']),assessments.get(a['package']))
        history=attempts.get(a['package'],[])
        latest=history[0] if history else {}
        successful=[d for d in history if d['status']=='downloaded']
        registered=sum(len(file_sets.get(d['id'],[])) for d in successful)
        app_rows.append([m.get('title'),a['package'],m.get('developer'),m.get('genre'),m.get('version'),m.get('score'),m.get('reviews'),m.get('ratings'),
            review_counts.get((a['package'],1),0),review_counts.get((a['package'],2),0),review_counts.get((a['package'],3),0),
            m.get('country'),m.get('language'),a.get('collected'),latest.get('status','not requested'),latest.get('architecture'),str(latest['version_code']) if latest.get('version_code') is not None else None,
            latest.get('error'),registered,', '.join(sorted({d['architecture'] for d in successful if file_sets.get(d['id'])})),
            'https://play.google.com/store/apps/details?'+urlencode({'id':a['package']}),release['label'],release['method'],'; '.join(e['source']+': '+e['text'] for e in release['evidence']),release.get('review',{}).get('note'),release.get('review',{}).get('source_url'),release.get('review',{}).get('observed_version')])
    sheet('Apps',['App name','Package ID','Developer','Category','Listing version','Average rating','Play Store reviews','Play Store ratings','Stored 1-star reviews','Stored 2-star reviews','Stored 3-star reviews','Country','Language','Metadata collected UTC','Latest APK status','Latest requested architecture','Latest APK version code','Latest error details','Registered APK files','Available architectures','Play Store URL','Testing evidence status','Testing assessment method','Automatic testing hints','Manual testing note','Testing source URL','Observed testing version'],app_rows)
    rows=[]
    for d in downloads:
        source=d.get('existing_download_id') or d['id']
        rows.append([metadata.get(d['package'],{}).get('title',d['package']),d['package'],d['id'],d['batch'],d['architecture'],d['status'],str(d['version_code']) if d.get('version_code') is not None else None,d.get('version_name'),d.get('created'),d.get('finished'),d.get('attempts',0),len(file_sets.get(source,[])),d.get('existing_download_id'),d.get('cancelled_by'),d.get('error')])
    sheet('APK downloads',['App name','Package ID','Download ID','Batch ID','Requested architecture','Status','APK version code','APK version name','Created UTC','Finished UTC','Attempts','Referenced file count','Referenced download ID','Cancelled by','Error details'],rows)
    rows=[]
    for f in files:
        d=by_id.get(f['download_id'],{})
        package=d.get('package','')
        rows.append([metadata.get(package,{}).get('title',package),package,f['id'],f['download_id'],str(d['version_code']) if d.get('version_code') is not None else None,d.get('version_name'),d.get('architecture'),PurePosixPath(f['relative_path']).name,f['kind'],f.get('split_name'),f['relative_path'],f['size_bytes'],round(f['size_bytes']/1048576,3),f['sha256'],d.get('finished')])
    ws=sheet('APK files',['App name','Package ID','File ID','Download ID','APK version code','APK version name','Requested architecture','File name','File kind','Split identity','Relative path','Size bytes','Size MB','SHA-256','Downloaded UTC'],rows)
    for row in range(2,ws.max_row+1):
        path=PurePosixPath(ws.cell(row,11).value)
        if not path.is_absolute() and '..' not in path.parts and path.parts and path.parts[0]=='apks' and '\\' not in str(path):
            cell=ws.cell(row,11)
            cell.hyperlink='./'+str(path)
            cell.font=Font(color='246B48',underline='single')
    output=io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
