"""S975 shared book inspection and GUI-bridge protocol (stdlib only).

Canonical source: Scripts-ByAI/ebook_translation_core.py. The plugin ships an
identical copy; delivery verification must compare their SHA-256 values.
"""
from __future__ import annotations
import hashlib
import importlib.util
import shutil
import html
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager, closing
from pathlib import Path
from xml.etree import ElementTree as ET
from html.parser import HTMLParser

SCHEMA = 1
ACTIVE = {'queued', 'starting', 'running', 'storing', 'needs_reconciliation'}
TERMINAL = {'completed', 'failed', 'rejected', 'interrupted'}
RELFILE = 'data/wasn-version-relations.json'
CHINESE = {'zh', 'zho', 'chi', 'zh-cn', 'zh-tw', 'zh-hans', 'zh-hant'}

class BridgeError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(value).hexdigest()

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default

@contextmanager
def serial(root):
    """An OS-owned lock: a crash releases it; a live lock is never deleted."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root/'bridge.lock').open('a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0'); f.flush()
        for attempt in range(50):
            try:
                f.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if attempt == 49:
                    raise BridgeError('橋接忙碌，請稍後再試')
                time.sleep(.02)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)

def inside(root, relative):
    root = Path(root).resolve()
    path = (root/relative).resolve()
    path.relative_to(root)
    return path

def lang(value):
    return str(value or '').lower().replace('_', '-')

def is_zh(value):
    return lang(value) in CHINESE or lang(value).startswith('zh-')

def epub_facts(path):
    """Inspect all text documents within explicit byte budgets; no extraction."""
    before=Path(path).stat()
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 10000:
            raise BridgeError('EPUB 檔案數超過檢查上限')
        if z.getinfo('META-INF/container.xml').file_size > 128000:
            raise BridgeError('EPUB container 過大')
        container = ET.fromstring(z.read('META-INF/container.xml'))
        opfs = [e.attrib.get('full-path') for e in container.iter() if e.tag.endswith('rootfile')]
        if len(opfs) != 1 or not opfs[0]:
            raise BridgeError('EPUB 主 OPF 不唯一')
        opf_info = z.getinfo(opfs[0])
        if opf_info.file_size > 2_000_000:
            raise BridgeError('EPUB OPF 過大')
        opf = ET.fromstring(z.read(opfs[0]))
        languages = [e.text or '' for e in opf.iter() if e.tag.endswith('}language')]
        texts = [i for i in infos if i.filename.lower().endswith(('.html', '.xhtml', '.htm'))]
        if not texts or sum(i.file_size for i in texts) > 24_000_000 or any(i.file_size > 4_000_000 for i in texts):
            raise BridgeError('EPUB 內文缺失或超過檢查上限')
        english = chinese = 0
        for info in texts:
            raw = z.read(info)
            try:
                node = ET.fromstring(raw)
                text = ' '.join(node.itertext())
            except ET.ParseError:
                class TextParser(HTMLParser):
                    def __init__(self):
                        super().__init__(convert_charrefs=True); self.parts=[]; self.hidden=0
                    def handle_starttag(self,tag,attrs):
                        if tag in ('script','style'): self.hidden+=1
                    def handle_endtag(self,tag):
                        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
                    def handle_data(self,value):
                        if not self.hidden: self.parts.append(value)
                parser=TextParser()
                parser.feed(raw.decode('utf-8'))
                text=' '.join(parser.parts)
            english += len(re.findall(r'[A-Za-z]', text))
            chinese += len(re.findall(r'[\u3400-\u9fff]', text))
        total = english+chinese
        sha=file_hash(path)
        after=Path(path).stat()
        if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):
            raise BridgeError('EPUB 檢查時發生變更')
        return {'languages': languages, 'latin': english, 'han': chinese,
                'han_ratio': chinese/max(1,total), 'sha256': sha}

def library_identity(library):
    root=Path(library).resolve(strict=True)
    with closing(sqlite3.connect((root/'metadata.db').as_uri()+'?mode=ro',uri=True)) as db:
        return str(root),str(db.execute('SELECT uuid FROM library_id').fetchone()[0])


def job_conflict(book, jobs):
    for job in jobs:
        if not isinstance(job,dict) or job.get('known') is not True:
            return 'Calibre 有身分未能核實的舊工作；請先核對並等候結束'
        if job['library_uuid']==book['library_uuid'] and (job['book_uuid']==book['uuid'] or job['book_id']==book['id']):
            return '這本書或同書號的前一版本仍在 Calibre 翻譯中'
    return ''


def historical_target(root,library,book_id,prior_id):
    prior=request_record(root,prior_id)
    path,lid=library_identity(library)
    if not prior or prior['library_uuid']!=lid or prior['library_path']!=path or prior['book_id']!=book_id:
        raise BridgeError('舊要求不屬於這個書庫與原始目標')
    return {'id':prior['book_id'],'uuid':prior['book_uuid'],'library_uuid':lid,'library_path':path}


def snapshot(library, book_id):
    root = Path(library).resolve(strict=True)
    with closing(sqlite3.connect((root/'metadata.db').as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,title,uuid,path FROM books WHERE id=?', (int(book_id),)).fetchone()
        if row is None:
            raise BridgeError('找不到這個書號')
        book = dict(row)
        book['library_uuid'] = str(db.execute('SELECT uuid FROM library_id').fetchone()[0])
        book['languages'] = [r[0] for r in db.execute('SELECT l.lang_code FROM languages l JOIN books_languages_link b ON b.lang_code=l.id WHERE b.book=?', (book_id,))]
        book['authors'] = [r[0] for r in db.execute('SELECT a.name FROM authors a JOIN books_authors_link b ON b.author=a.id WHERE b.book=?',(book_id,))]
        book['tags'] = [r[0] for r in db.execute('SELECT t.name FROM tags t JOIN books_tags_link b ON b.tag=t.id WHERE b.book=?',(book_id,))]
        book['identifiers'] = dict(db.execute('SELECT type,val FROM identifiers WHERE book=?',(book_id,)))
        cm = db.execute('SELECT text FROM comments WHERE book=?',(book_id,)).fetchone()
        book['comments'] = cm[0] if cm else ''
        formats = list(db.execute('SELECT format,name FROM data WHERE book=?',(book_id,)))
        try:
            count=db.execute('SELECT count(*) FROM annotations WHERE book=?',(book_id,)).fetchone()[0]
            positions=db.execute("SELECT count(*) FROM last_read_positions WHERE book=? AND upper(format)='EPUB'",(book_id,)).fetchone()[0]
            book['reading_assets']={'known':True,'annotations':count,'last_read_positions':positions}
        except sqlite3.Error:
            book['reading_assets']={'known':False,'annotations':None,'last_read_positions':None}
    directory = inside(root, book['path'])
    book['directory'] = str(directory)
    book['library_path'] = str(root)
    book['formats'] = [r[0].upper() for r in formats]
    epubs = [inside(directory,r[1]+'.epub') for r in formats if r[0].upper()=='EPUB']
    book['epub'] = str(epubs[0]) if len(epubs)==1 else None
    book['relations'] = read_json(directory/RELFILE, {'schema':SCHEMA,'library_uuid':book['library_uuid'],'book_uuid':book['uuid'],'relations':[]})
    if not isinstance(book['relations'],dict) or not isinstance(book['relations'].get('relations'),list):
        raise BridgeError('版本關係紀錄損壞，未放行翻譯')
    if book['relations'].get('library_uuid')!=book['library_uuid'] or book['relations'].get('book_uuid')!=book['uuid']:
        raise BridgeError('版本關係檔屬於另一筆書，未放行')
    book['facts'] = epub_facts(book['epub']) if book['epub'] else None
    return book

def verified_variants(book):
    if '_verified_graph' in book: return book['_verified_graph']
    results=[]
    directory = Path(book['directory'])
    manifests=list((directory/'data'/'translation-variants').glob('*/manifest.json'))
    # Older linked_pair layouts keep the manifest and bilingual attachment on
    # the translated record, linked by the original's UUID (not title guessing).
    main_paths={book['id']:book.get('epub')}
    owners={book['id']:{'book_id':book['id'],'book_uuid':book['uuid'],'title':book['title'],'directory':book['directory']}}
    library=Path(book['library_path'])
    with closing(sqlite3.connect((library/'metadata.db').as_uri()+'?mode=ro',uri=True)) as db:
        related=db.execute("SELECT DISTINCT b.id,b.path,b.uuid,b.title FROM books b JOIN identifiers i ON i.book=b.id WHERE i.type='ebook-translator-source' AND i.val=?",(book['uuid'],)).fetchall()
        for bid,relative,buid,title in related:
            other=inside(library,relative)
            owners[bid]={'book_id':bid,'book_uuid':buid,'title':title,'directory':str(other)}
            manifests.extend((other/'data'/'translation-variants').glob('*/manifest.json'))
            fmt=db.execute("SELECT name FROM data WHERE book=? AND upper(format)='EPUB'",(bid,)).fetchall()
            if len(fmt)==1: main_paths[bid]=str(inside(other,fmt[0][0]+'.epub'))
    book['_linked_owners']=[owners[key] for key in sorted(owners) if key!=book['id']]
    for p in sorted(set(manifests)):
        p.resolve().relative_to(library)
        manifest_raw=p.read_bytes()
        m=json.loads(manifest_raw)
        if not isinstance(m,dict):
            raise BridgeError('翻譯 manifest 無法讀取')
        is_source=str(m.get('source_uuid'))==book['uuid']
        is_derivative=(book['id']!=m.get('source_book_id') and
                       book['id'] in (m.get('derivative_book_ids') or {}).values() and
                       book['identifiers'].get('ebook-translator-source')==str(m.get('source_uuid')))
        if not (is_source or is_derivative):
            raise BridgeError('翻譯 manifest 與書籍身分不符')
        if not (is_zh(m.get('target_language_code')) or re.search(r'chinese|中文|繁體|简体|簡體',str(m.get('target_language','')),re.I)):
            continue
        roles={}
        hashes=m.get('sha256') or {}
        main = book['facts']['sha256'] if book.get('facts') else ''
        for role in ('source','translation','bilingual'):
            expected=hashes.get(role)
            if not re.fullmatch('[a-f0-9]{64}',str(expected or '')):
                raise BridgeError('翻譯 manifest 缺少可驗證 SHA')
            matches=[f for f in p.parent.glob(role+'*.epub') if file_hash(f)==expected]
            for f in matches:
                f.resolve().relative_to(library)
            if expected==main:
                roles[role]=book['epub']
            elif len(matches)==1:
                roles[role]=str(matches[0])
            else:
                derivative_id=(m.get('derivative_book_ids') or {}).get(role)
                other_main=main_paths.get(derivative_id)
                if other_main and file_hash(other_main)==expected:
                    roles[role]=other_main
        if 'translation' in roles or 'bilingual' in roles:
            results.append({'manifest':m,'manifest_path':str(p),'manifest_sha256':digest(manifest_raw),'roles':roles,
                            'owners':[owners[key] for key in sorted(owners)]})
        else:
            raise BridgeError('翻譯紀錄存在，但中譯／雙語檔缺失或 SHA 不符')
    book['_verified_graph']=results
    return results

def note_paths(library, h2o_root, book, pattern='*.md'):
    if h2o_root is None or not Path(h2o_root).is_dir(): raise BridgeError('H2O 目錄尚未設定或無法存取，未猜測落點')
    root=Path(h2o_root).resolve()
    source_pattern=re.compile(r'^[ \t]*-[ \t]*Source[ \t]*::.*?calibre://(?:show|view)-book/([^/)]+)/(\d+)',re.I|re.M)
    out=[]
    for p in root.rglob(pattern):
        p.resolve().relative_to(root)
        m=source_pattern.search(p.read_text(encoding='utf-8'))
        if m and int(m.group(2))==book['id']:
            library_name=m.group(1)
            if library_name.startswith('_hex_-'): library_name=bytes.fromhex(library_name[6:]).decode('utf-8')
            if library_name!=Path(library).name: raise BridgeError('H2O Source 指向另一個書庫，未猜測落點')
            out.append(p)
    if len(out)>1: raise BridgeError('本書有多份 H2O；請先指定版本，不猜落點')
    return out


def note_plan(library,h2o_root,book):
    paths=note_paths(library,h2o_root,book)
    if paths:
        p=paths[0]
        return {'kind':'h2o','path':str(p.resolve()),'label':p.name,'before_sha256':file_hash(p),
                'identity_limit':'舊 Source 只有書庫名稱與書號，沒有 UUID；請核對這是本書筆記'}
    return {'kind':'comments','label':'Calibre 簡介最前方','before_sha256':digest((book['comments'] or '').encode()),'identity_limit':''}


def relation_token(book,plan):
    return digest(json.dumps({'book_token':confirmation_token(book),'destination':plan},ensure_ascii=False,sort_keys=True).encode())


def default_note_writer(path,before,after):
    primitive=Path(__file__).with_name('migrate_h2o_kangxi_s796.py')
    if not primitive.is_file(): raise BridgeError('未設定既有 H2O GuardedWrite primitive')
    spec=importlib.util.spec_from_file_location('_wasn_s975_note_atomic',primitive)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.atomic_replace(Path(path),before,after)


def availability(book, note_root=None):
    facts=book.get('facts')
    base={'book_id':book['id'],'book_uuid':book['uuid'],'library_uuid':book['library_uuid'],
          'title':book['title'],'can_translate':False,'can_archive':False}
    def result(state, reason, **more):
        return dict(base,state=state,reason=reason,**more)
    confirmed=[r for r in book['relations']['relations'] if r.get('decision')=='same' and r.get('library_uuid')==book['library_uuid']]
    if confirmed:
        for record in confirmed:
            visible=record.get('visible')
            if not visible: continue
            if visible['kind']=='comments': actual=(book['comments'] or '').encode()
            else:
                if note_root is None: return result('uncertain','版本關係的 H2O 目前無法核對，請檢查或撤銷本側記錄')
                p=Path(visible['path']).resolve();p.relative_to(Path(note_root).resolve())
                actual=p.read_bytes() if p.exists() else b''
            if actual.count(visible['inserted'].encode())!=1:
                return result('uncertain','版本關係的可見行已被修改或移除，請核對或撤銷本側記錄')
        chinese=bool(facts and facts['han']>=50 and facts['han_ratio']>.2)
        label='已確認英文原書：' if chinese else '已確認中文版：'
        return result('chinese' if chinese else 'translated',label+ '、'.join(f"{r['peer_title']}（#{r['peer_id']}）" for r in confirmed))
    variants=verified_variants(book)
    if not variants and book.get('_linked_owners'): return result('uncertain','找到另存版本的來源 UUID 標記，但缺可驗證版本紀錄，請先核對')
    if facts:
        retained=[v for v in variants if (v['manifest'].get('effective_storage') or v['manifest'].get('requested_storage')) in ('linked_pair','three_records') and v['manifest'].get('source_uuid')==book['uuid'] and v['manifest']['sha256'].get('source')==facts['sha256']]
        if retained: return result('translated','英文主檔已刻意保留，已有另存譯本；不再提供翻譯或整理',source_sha256=facts['sha256'])
        archive=[v for v in variants if 'bilingual' in v['roles'] and v['manifest']['sha256'].get('source')==facts['sha256']]
        if len(archive)==1:
            v=archive[0]
            return result('archive','英文原檔仍為主檔；已有可驗證雙語版，可整理原檔',can_archive=True,
                          source_sha256=facts['sha256'],archive=v,reading_assets=book['reading_assets'],
                          requires_reading_consent=(not book['reading_assets']['known'] or bool(book['reading_assets']['annotations'] or book['reading_assets']['last_read_positions'])))
        if variants:
            return result('translated','已有可驗證的中譯／雙語 EPUB',source_sha256=facts['sha256'])
    tags=set(book['tags'])
    marked=tags.intersection({'中譯_','雙語_','附雙語_','附中譯_'}) or any(k in book['identifiers'] for k in ('ebook-translator-variant','ebook-translator-source'))
    if marked:
        return result('translated','已有中譯／雙語標記，停止重複翻譯（舊書附件仍需人工核對）')
    langs=book['languages']+(facts['languages'] if facts else [])
    if facts and facts['han']>=50 and facts['han_ratio']>.2:
        return result('chinese','中文書，無需翻譯')
    if not facts:
        return result('unsupported','格式未支援：目前整本翻譯入口支援 EPUB；本書無 EPUB')
    if any(is_zh(v) for v in langs):
        return result('uncertain','中文語言標記與可驗證內文不一致，請先核對')
    if facts['han']>50 and facts['han_ratio']>.015:
        return result('uncertain','內文含可觀中文，可能已有譯文，請先核對')
    if facts['latin']<200 or not any(lang(v) in ('en','eng','en-us','en-gb') for v in langs):
        return result('uncertain','無法確認為英文原書，請先核對語言')
    return result('eligible','英文原書，尚未找到本書譯本',can_translate=True,source_sha256=facts['sha256'])

def candidates(library, book, note_root=None):
    """Suggestions only: even an exact source UUID is shown for confirmation."""
    omitted={r.get('peer_uuid') for r in book['relations']['relations'] if r.get('decision') in ('different','same')}
    normalized=lambda s: re.sub(r'[^\w]','',s.lower())
    title=normalized(book['title'])
    authors={normalized(a) for a in book['authors']}
    out=[]
    root=Path(library)
    with closing(sqlite3.connect((root/'metadata.db').as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('''SELECT DISTINCT b.id,b.uuid,b.title,COALESCE(c.text,'') FROM books b JOIN books_languages_link bl ON bl.book=b.id JOIN languages l ON l.id=bl.lang_code LEFT JOIN comments c ON c.book=b.id WHERE l.lang_code IN ('zho','chi','zh','zh_CN','zh_TW') AND b.id!=?''',(book['id'],)).fetchall()
        authors_by_id={}
        for bid,author in db.execute('SELECT ba.book,a.name FROM books_authors_link ba JOIN authors a ON a.id=ba.author'):
            authors_by_id.setdefault(bid,set()).add(normalized(author))
        sources=dict(db.execute("SELECT book,val FROM identifiers WHERE type='ebook-translator-source'"))
        for bid,buid,name,comments in rows:
            if buid in omitted: continue
            aa=authors_by_id.get(bid,set())
            source=sources.get(bid)
            reasons=[]
            if source==book['uuid']: reasons.append('外掛來源 UUID 相同')
            same_author=bool(authors.intersection(aa))
            title_in_comments=len(title)>10 and title in normalized(html.unescape(re.sub('<[^>]+>',' ',comments)))
            title_in_epub=False
            if same_author and not title_in_comments:
                rel=db.execute("SELECT b.path,d.name FROM books b JOIN data d ON d.book=b.id WHERE b.id=? AND upper(d.format)='EPUB'",(bid,)).fetchall()
                if len(rel)==1:
                    path=inside(inside(root,rel[0][0]),rel[0][1]+'.epub')
                    try:
                        with zipfile.ZipFile(path) as z:
                            budget=0
                            docs=[i for i in z.infolist() if i.filename.lower().endswith(('.xhtml','.html','.htm'))][:12]
                            for info in docs:
                                budget+=info.file_size
                                if budget>2_000_000: break
                                text=z.read(info).decode('utf-8',errors='strict')
                                if len(title)>10 and title in normalized(html.unescape(re.sub('<[^>]+>',' ',text))):
                                    title_in_epub=True;break
                    except (OSError,ValueError,zipfile.BadZipFile,UnicodeError):
                        pass  # A suggestion is optional; never infer a match from a read failure.
            if same_author and (title_in_comments or title_in_epub):
                reasons.append('作者相同，'+('書介' if title_in_comments else 'EPUB 前置頁')+'含英文原書名')
            if reasons: out.append({'id':bid,'uuid':buid,'title':name,'reasons':reasons})
    displayed=[]
    for item in sorted(out,key=lambda r:(-len(r['reasons']),r['id']))[:12]:
        peer=snapshot(library,item['id'])
        if peer['uuid']!=item['uuid'] or peer['title']!=item['title'] or not chinese_peer(peer): continue
        if note_root is not None:
            plan=note_plan(library,note_root,peer)
            item['destination']=plan;item['peer_token']=relation_token(peer,plan)
        else: item['peer_token']=confirmation_token(peer)
        displayed.append(item)
    return displayed

def public_state(library, book_id, note_root=None):
    try:
        book=snapshot(library,book_id)
        state=availability(book,note_root=note_root)
        archived=state.pop('archive',None)
        if archived:
            asset=Path(archived['roles']['bilingual'])
            owner=next((item for item in archived['owners'] if asset.is_relative_to(Path(item['directory']))),None)
            compact=asset.relative_to(Path(owner['directory'])).as_posix() if owner else '書庫內：'+asset.relative_to(Path(library).resolve()).as_posix()
            if owner and owner['book_id']!=book['id']: compact=f"書籍 #{owner['book_id']}："+compact
            state['archive_preview']={'source':book['epub'],'source_sha256':state['source_sha256'],
                'destination':'data/translation-variants/<本次新建目錄>/source.epub',
                'new_main':archived['roles']['bilingual'],
                'new_main_sha256':archived['manifest']['sha256']['bilingual'],
                'source_relative':Path(book['epub']).relative_to(book['directory']).as_posix(),
                'new_main_relative':compact,'new_main_owner':owner}
        state['candidates']=candidates(library,book,note_root=note_root) if state['state']=='eligible' else []
        state['relations']=book['relations']['relations']
        state['source_token']=confirmation_token(book)
        if note_root is not None:
            plan=note_plan(library,note_root,book)
            state['destination']=plan;state['relation_source_token']=relation_token(book,plan)
        return state
    except (OSError,ValueError,KeyError,RuntimeError,sqlite3.Error,zipfile.BadZipFile,ET.ParseError) as exc:
        return {'book_id':book_id,'state':'uncertain','can_translate':False,'can_archive':False,'reason':str(exc)}

def bridge_health(root, book):
    hb=read_json(Path(root)/'heartbeat.json',{})
    connected=(hb.get('schema')==SCHEMA and hb.get('library_uuid')==book.get('library_uuid') and
               0 <= time.time()-float(hb.get('at',0)) < 15)
    return {'connected':connected,'ready':connected and hb.get('ready') is True,
            'reason':('' if hb.get('ready') else hb.get('reason','請先設定翻譯引擎')) if connected else '翻譯外掛橋接未連線',
            'instance':hb.get('instance'),'pid':hb.get('pid'),'manual_jobs':hb.get('manual_jobs',[]),'h2o_root':hb.get('h2o_root')}


def requests(root):
    for p in sorted((Path(root)/'requests').glob('*.json')):
        req=read_json(p)
        if not isinstance(req,dict) or req.get('schema')!=SCHEMA:
            raise BridgeError('橋接佇列資料損壞')
        yield p,req


def request_record(root,rid):
    return read_json(Path(root)/'requests'/(rid+'.json')) or read_json(Path(root)/'history'/(rid+'.json'))


def write_request(root,req):
    root=Path(root)
    if req.get('state') in TERMINAL:
        atomic_json(root/'history'/(req['id']+'.json'),req)
        (root/'requests'/(req['id']+'.json')).unlink(missing_ok=True)
    else: atomic_json(root/'requests'/(req['id']+'.json'),req)


def maintain(root, backup_root=None, now=None):
    now=time.time() if now is None else now
    for p in (Path(root)/'history').glob('*.json'):
        value=read_json(p)
        if value.get('state') in TERMINAL and now-value.get('updated_at',now)>90*86400:p.unlink()
    if backup_root:
        for p in Path(backup_root).glob('*/manifest.json'):
            if not re.fullmatch('[a-f0-9]{32}',p.parent.name): continue
            value=read_json(p);resolved=value.get('resolved_at')
            req=request_record(root,p.parent.name)
            if resolved and now-resolved>=30*86400 and (not req or req.get('state') in TERMINAL):
                base=Path(backup_root).resolve();target=p.parent.resolve()
                relative=target.relative_to(base)
                if len(relative.parts)!=1 or relative.name!=p.parent.name: raise BridgeError('備份清理路徑超出預定根，未刪除')
                shutil.rmtree(target)


def mark_conflict_resolved(backup_root,rid,*,acknowledged=False):
    if acknowledged is not True or not re.fullmatch('[a-f0-9]{32}',rid): raise BridgeError('必須先明示核對並解決備份衝突')
    p=Path(backup_root)/rid/'manifest.json';value=read_json(p)
    if not value: raise BridgeError('找不到衝突備份')
    value['resolved_at']=time.time();atomic_json(p,value)


def process_alive(pid):
    """Conservative three-state liveness. Never use os.kill on Windows."""
    if type(pid) is not int or pid<=0: return None
    if pid==os.getpid(): return True
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        handle=kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE only
        if not handle: return False if ctypes.get_last_error()==87 else None
        try:
            result=kernel.WaitForSingleObject(handle,0)
            return {0:False,258:True}.get(result)
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0); return True
    except ProcessLookupError: return False
    except PermissionError: return None


def request_view(root, req, health):
    value=dict(req)
    event=read_json(Path(root)/'outcomes'/(req['id']+'.json'))
    if event and event.get('instance')==req.get('instance'):
        value.update(event)
    if value.get('state') in {'starting','running','storing','needs_reconciliation'}:
        if not health['connected'] or health['instance']!=value.get('instance'):
            value.update(state='needs_reconciliation',detail='原工作擁有者未連線；請核對 Calibre 工作，不會自動重送')
    return value


def book_evidence(book):
    # Bind both displayed identity and content; language metadata alone cannot
    # establish a Chinese edition. A relation record is not recursive evidence.
    facts=book.get('facts')
    return {'library_uuid':book['library_uuid'],'library_path':book['library_path'],
            'uuid':book['uuid'],'title':book['title'],'authors':sorted(book['authors']),'languages':sorted(book['languages']),
            'sha256':facts['sha256'] if facts else None,
            'epub_languages':facts['languages'] if facts else []}


def confirmation_token(book):
    # The exact graph used for availability/preview is frozen on this snapshot.
    # Never reread a different valid graph while minting its display token.
    root=Path(book['library_path']).resolve()
    graph=[]
    for variant in verified_variants(book):
        graph.append({'manifest':Path(variant['manifest_path']).relative_to(root).as_posix(),
                      'manifest_sha256':variant['manifest_sha256'],
                      'owners':[dict(owner,directory=Path(owner['directory']).relative_to(root).as_posix()) for owner in variant['owners']],
                      'roles':{role:{'path':Path(path).relative_to(root).as_posix(),'sha256':variant['manifest']['sha256'][role]}
                               for role,path in sorted(variant['roles'].items())}})
    value={'book':book_evidence(book),'comments_sha256':digest((book['comments'] or '').encode()),
           'tags':sorted(book['tags']),'identifiers':book['identifiers'],'relations':book['relations'],
           'verified_asset_graph':graph,'reading_assets':book['reading_assets'],
           'linked_records':[dict(owner,directory=Path(owner['directory']).relative_to(root).as_posix()) for owner in book.get('_linked_owners',[])]}
    return digest(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())


# Kept as a named compatibility helper for diagnostic probes.
peer_evidence=book_evidence


def chinese_peer(book):
    facts=book.get('facts')
    if facts and facts['han']>=50 and facts['han_ratio']>.2: return True
    return any(any(role in v['roles'] for role in ('translation','bilingual')) for v in verified_variants(book))


def enqueue(root, library, book_id, operation, *, source_sha256=None, reading_policy=None, peer_id=None, decision=None, request_id=None, pair_id=None, prior_request_id=None, source_token=None, peer_token=None, reading_consent=False):
    if type(reading_consent) is not bool: raise BridgeError('替換主檔同意值必須是明確布林')
    if operation not in ('translate','archive','relation','undo_relation','reconcile'):
        raise BridgeError('未知操作')
    # EPUB parsing and hashing must never hold the cross-process queue lock.
    if operation=='reconcile':
        if not re.fullmatch('[a-f0-9]{32}',prior_request_id or ''): raise BridgeError('待核對要求 ID 無效')
        book=historical_target(root,library,book_id,prior_request_id);state={}
    else:
        book=snapshot(library,book_id);state=availability(book,note_root=read_json(Path(root)/'heartbeat.json',{}).get('h2o_root'))
        source_plan=note_plan(library,read_json(Path(root)/'heartbeat.json',{}).get('h2o_root'),book) if operation=='relation' else None
        expected=relation_token(book,source_plan) if operation=='relation' else confirmation_token(book)
        if operation in ('translate','archive','relation') and source_token!=expected:
            raise BridgeError('待核對：畫面所確認的原書或落點已變更或缺少確認證據，請重新開啟')
    health=bridge_health(root,book)
    if not health['connected']: raise BridgeError(health['reason'])
    if operation=='translate' and not health['ready']: raise BridgeError(health['reason'])
    conflict=job_conflict(book,health['manual_jobs'])
    if conflict: raise BridgeError(conflict)
    if operation=='translate' and not state['can_translate']: raise BridgeError(state['reason'])
    if operation=='archive' and not state['can_archive']: raise BridgeError(state['reason'])
    if operation=='archive' and state.get('requires_reading_consent') and not reading_consent: raise BridgeError('有畫線、閱讀進度或無法核實；須在裝置明示同意替換主檔')
    if operation in ('translate','archive') and source_sha256 != state.get('source_sha256'):
        raise BridgeError('原書已變更，請重新開啟詳細頁')
    if operation=='translate' and reading_policy not in ('linked_pair','replace_main'):
        raise BridgeError('請先選擇閱讀資產的保留方式')
    peer=None
    if operation=='relation':
        if type(peer_id) is not int or peer_id<=0 or peer_id==book_id or decision not in ('same','different'):
            raise BridgeError('版本配對選項無效')
        peer=snapshot(library,peer_id)
        conflict=job_conflict(peer,health['manual_jobs'])
        if conflict: raise BridgeError(conflict)
        peer_plan=note_plan(library,health['h2o_root'],peer)
        if peer_token!=relation_token(peer,peer_plan): raise BridgeError('待核對：畫面所確認的對照版本或落點已變更')
        if decision=='same' and not chinese_peer(peer): raise BridgeError('無法驗證對照書為中文版，請先核對')
    active_uuids={book['uuid']} | ({peer['uuid']} if peer else set())
    rid=request_id or uuid.uuid4().hex
    if not re.fullmatch('[a-f0-9]{32}',rid): raise BridgeError('request ID 無效')
    if operation=='reconcile' and not re.fullmatch('[a-f0-9]{32}',prior_request_id or ''):
        raise BridgeError('待核對要求 ID 無效')
    with serial(root):
        queue=list(requests(root))
        historical=read_json(Path(root)/'history'/(rid+'.json'))
        if historical: queue.append((None,historical))
        for _,old in queue:
            if old['id']==rid:
                if (old['library_uuid']!=book['library_uuid'] or old['book_uuid']!=book['uuid'] or old['operation']!=operation or
                    any(old.get(k)!=v for k,v in {'source_sha256':source_sha256,'reading_policy':reading_policy,'peer_id':peer_id,'decision':decision,'pair_id':pair_id,'prior_request_id':prior_request_id,'source_token':source_token,'peer_token':peer_token,'reading_consent':reading_consent}.items())):
                    raise BridgeError('request ID 衝突')
                return request_view(root,old,health)
            if old['library_uuid']!=book['library_uuid']: continue
            identities={old['book_uuid']} | ({old['peer_uuid']} if old.get('peer_uuid') else set())
            viewed=request_view(root,old,health)
            if viewed.get('state') in ACTIVE and active_uuids.intersection(identities):
                if operation=='reconcile' and old['id']==prior_request_id and viewed['state']=='needs_reconciliation': continue
                if old['operation']==operation and old['book_uuid']==book['uuid'] and operation in ('translate','archive'):
                    return viewed
                raise BridgeError('本書已有待處理操作，請等候或核對結果')
        req={'schema':SCHEMA,'id':rid,'operation':operation,'state':'queued','created_at':time.time(),
             'expires_at':time.time()+120,'instance':health['instance'],'owner_pid':health['pid'],'library_uuid':book['library_uuid'],
             'library_path':str(Path(library).resolve()),'book_id':int(book_id),'book_uuid':book['uuid'],
             'book_ids':sorted({book_id} | ({peer_id} if peer else set())),
             'source_sha256':source_sha256,'reading_policy':reading_policy,
             'peer_id':peer_id,'peer_uuid':peer['uuid'] if peer else None,'peer_evidence':peer_evidence(peer) if peer else None,
             'decision':decision,'pair_id':pair_id,'prior_request_id':prior_request_id,
             'source_token':source_token,'peer_token':peer_token,'reading_consent':reading_consent,
             'source_evidence':book_evidence(book) if operation!='reconcile' else None,
             'note_plans':{'source':source_plan,'peer':peer_plan} if operation=='relation' else None}
        atomic_json(Path(root)/'requests'/(rid+'.json'),req)
        return req

class Consumer:
    """Called only on the Calibre Qt main thread; API process only enqueues."""
    def __init__(self, root, library, api, start_translation, *, h2o_root=None, readiness=None, manual_jobs=None, note_writer=None, backup_root=None):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.library=Path(library).resolve(); self.api=api
        self.start_translation=start_translation; self.h2o_root=Path(h2o_root).resolve() if h2o_root else None
        self.readiness=readiness or (lambda: (True,''))
        self.manual_jobs=manual_jobs or (lambda: [])
        self.note_writer=note_writer or default_note_writer
        self.backup_root=Path(backup_root) if backup_root else None
        self.last_maintenance=0
        self.instance=uuid.uuid4().hex
        with closing(sqlite3.connect((self.library/'metadata.db').as_uri()+'?mode=ro',uri=True)) as db:
            self.library_uuid=str(db.execute('SELECT uuid FROM library_id').fetchone()[0])
        self.last_heartbeat=0

    def heartbeat(self):
        ready,reason=self.readiness()
        atomic_json(self.root/'heartbeat.json',{'schema':SCHEMA,'library_uuid':self.library_uuid,'at':time.time(),
                    'instance':self.instance,'pid':os.getpid(),'h2o_root':str(self.h2o_root) if self.h2o_root else None,'ready':ready,'reason':reason,'manual_jobs':self.manual_jobs()})

    def update(self, req, state, **fields):
        req.update(state=state,updated_at=time.time(),**fields)
        write_request(self.root,req)

    def event(self, req, state, **fields):
        # A callback must not wait for the queue lock after a GUI job or storage
        # has succeeded. Durable single-writer outcome is drained on next tick.
        req.update(state=state,updated_at=time.time(),**fields)
        atomic_json(self.root/'outcomes'/(req['id']+'.json'),req)

    def finish(self, req, success, detail=''):
        self.event(req,'completed' if success else 'failed',detail=detail)

    def reconcile(self, req, book):
        old=request_record(self.root,req['prior_request_id'])
        if (not old or old['library_uuid']!=book['library_uuid'] or old['book_uuid']!=book['uuid'] or
            old['instance']==self.instance): raise BridgeError('舊要求身分不符或仍屬目前工作擁有者')
        old=request_view(self.root,old,bridge_health(self.root,book))
        if old['state']!='needs_reconciliation': raise BridgeError('舊要求已有結果，請重新整理')
        jobs=self.manual_jobs()
        for bid,buid in [(old['book_id'],old['book_uuid'])]+([(old['peer_id'],old['peer_uuid'])] if old.get('peer_uuid') else []):
            conflict=job_conflict({'id':bid,'uuid':buid,'library_uuid':old['library_uuid']},jobs)
            if conflict: raise BridgeError(conflict)
        alive=process_alive(old.get('owner_pid'))
        if old.get('owner_pid')!=os.getpid() and alive is not False:
            raise BridgeError('原 Calibre 程序仍在執行或無法證明已結束，保留鎖定')
        self.update(old,'interrupted',detail='使用者已核對，舊要求已解除；未自動重送。請另外核對可能的部分寫入；解除不代表已回復版本關係或資料',resolved_by=req['id'])

    def tick(self):
        self.heartbeat()
        if time.time()-self.last_maintenance>3600:
            maintain(self.root,self.backup_root);self.last_maintenance=time.time()
        try:
            with serial(self.root):
                for p in (self.root/'outcomes').glob('*.json'):
                    event=read_json(p)
                    old=request_record(self.root,p.stem)
                    if old and event.get('instance')==old.get('instance'):
                        write_request(self.root,event)
                        p.unlink()
                for _,old in requests(self.root):
                    if old['library_uuid']==self.library_uuid and old['instance']!=self.instance and old.get('state') in {'starting','running','storing'}:
                        self.update(old,'needs_reconciliation',detail='工作擁有者已變更；結果待核對，不會自動重送')
                pending=next(((p,r) for p,r in requests(self.root) if r.get('state')=='queued' and r['library_uuid']==self.library_uuid),None)
                if pending is None: return
                _,req=pending
                if req['instance']!=self.instance or time.time()>req['expires_at']:
                    detail='外掛世代已變更；未啟動' if req['instance']!=self.instance else '要求已逾期（Calibre 對話框或手動流程可能尚未結束）；未啟動'
                    self.update(req,'rejected',detail=detail)
                    return
                self.update(req,'starting')
        except BridgeError as exc:
            if str(exc)!='橋接忙碌，請稍後再試':
                heartbeat=read_json(self.root/'heartbeat.json',{})
                heartbeat.update(ready=False,reason=str(exc))
                atomic_json(self.root/'heartbeat.json',heartbeat)
            return  # Busy lock retries; corrupt queue is visible in heartbeat.
        try:
            if req['operation']=='reconcile':
                book=historical_target(self.root,self.library,req['book_id'],req['prior_request_id'])
                with serial(self.root): self.reconcile(req,book)
                self.finish(req,True,'舊要求已核對解除；部分寫入仍需核對，未自動重送')
                return
            book=snapshot(self.library,req['book_id'])
            if (book['library_uuid']!=req['library_uuid'] or book['uuid']!=req['book_uuid'] or
                str(self.library)!=req['library_path']):
                raise BridgeError('目前書庫或書籍身分與要求不符')
            conflict=job_conflict(book,self.manual_jobs())
            if conflict: raise BridgeError(conflict)
            expected=relation_token(book,note_plan(self.library,self.h2o_root,book)) if req['operation']=='relation' else confirmation_token(book)
            if req['operation'] in ('translate','archive','relation') and expected!=req['source_token']:
                raise BridgeError('待核對：原書的已確認內容、落點或版本資產已變更，未啟動')
            op=req['operation']
            if op=='translate':
                ready,reason=self.readiness()
                if not ready: raise BridgeError(reason)
            if op in ('translate','archive'):
                st=availability(book)
                if req['source_sha256']!=st.get('source_sha256'): raise BridgeError('主檔已變更')
                if not st['can_'+op]: raise BridgeError(st['reason'])
            if op=='translate':
                def done(success,detail=''): self.finish(req,success,detail)
                done.bridge_root=self.root
                done.deferred=lambda:self.event(req,'storing',detail='翻譯已完成，等候 Calibre 對話框關閉後存回')
                done.storing=lambda:self.event(req,'storing',detail='翻譯已完成，正在存回 Calibre')
                job=self.start_translation(req,book,done)
                if job is None: raise BridgeError('外掛未建立 GUI job')
                if req['state'] not in TERMINAL:
                    self.event(req,'running',job_id=str(getattr(job,'id','')),detail='Calibre GUI job 已建立')
            elif op=='archive':
                self.archive(book,st['archive'],reading_consent=req.get('reading_consent',False))
                self.finish(req,True,'英文原檔已收進 Data，雙語版已成主 EPUB')
            elif op=='relation':
                peer=snapshot(self.library,req['peer_id'])
                if relation_token(peer,note_plan(self.library,self.h2o_root,peer))!=req['peer_token'] or book_evidence(peer)!=req['peer_evidence']: raise BridgeError('待核對：對照版本、落點或 before_sha 已變更，未寫入')
                conflict=job_conflict(peer,self.manual_jobs())
                if conflict: raise BridgeError(conflict)
                if req['decision']=='same' and not chinese_peer(peer): raise BridgeError('對照書已無可驗證中文版，未寫入')
                self.relation(book,peer,req['decision'],expected_plans=req['note_plans'])
                self.finish(req,True,'兩側版本關係已記錄，可各自撤銷')
            elif op=='undo_relation':
                removed=self.undo_relation(book,req.get('pair_id'))
                self.finish(req,True,'本側版本關係已撤銷，其他內容保留' if removed else '僅撤銷本側內部關係；原版本行缺失或改過，請人工核對，筆記／簡介未改')
            else: raise BridgeError('未知操作')
        except Exception as exc:
            self.finish(req,False,str(exc))

    def add_data(self, book_id, relpath, data, *, replace=True):
        with tempfile.TemporaryDirectory(dir=self.root) as td:
            staged=Path(td)/'payload'
            staged.write_bytes(data)
            self.api.add_extra_files(book_id,{relpath:str(staged)},replace=replace,auto_rename=False)
            stored={x.relpath:Path(x.file_path) for x in self.api.list_extra_files(book_id,use_cache=False)}
            if relpath not in stored or stored[relpath].read_bytes()!=data:
                raise BridgeError('Calibre Data 寫入後驗證失敗')

    def archive(self,book,variant,*,reading_consent=False):
        if (variant['manifest'].get('effective_storage') or variant['manifest'].get('requested_storage')) in ('linked_pair','three_records'):
            raise BridgeError('英文主檔已刻意保留；不提供整理')
        try:
            count=self.api.annotation_count_for_book(book['id'])
            positions=self.api.get_last_read_positions(book['id'],fmt='EPUB',limit=1)
            if type(count) is not int or not isinstance(positions,(list,tuple)): raise ValueError('reading assets unknown')
            needs_consent=count>0 or bool(positions)
        except Exception: needs_consent=True
        if needs_consent and reading_consent is not True:
            raise BridgeError('待核對：Calibre 有畫線/進度或無法證明沒有，未替換主檔；請先明示同意')
        # The existing bundle_data sequence: preserve+verify source, replace,
        # verify, then mark. Never filesystem-move/delete a library file.
        source=Path(book['epub']); bilingual=Path(variant['roles']['bilingual'])
        run_id=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
        prefix='data/translation-variants/'+run_id+'/'
        original_ids=dict(self.api.field_for('identifiers',book['id']) or {})
        original_tags=list(self.api.field_for('tags',book['id']) or [])
        manifest=dict(variant['manifest'],run_id=run_id,main_variant='bilingual',effective_storage='bundle_data',
                      source_book_id=book['id'],source_uuid=book['uuid'],derivative_book_ids={'bilingual':book['id'],'translation':None})
        with tempfile.TemporaryDirectory(dir=self.root) as td:
            backup=Path(td)/'source.epub'; backup.write_bytes(source.read_bytes())
            if file_hash(backup)!=book['facts']['sha256']: raise BridgeError('原檔快照 SHA 不符')
            self.add_data(book['id'],prefix+'source.epub',backup.read_bytes(),replace=False)
            if 'translation' in variant['roles']:
                self.add_data(book['id'],prefix+'translation.zh.epub',Path(variant['roles']['translation']).read_bytes(),replace=False)
            try:
                if self.api.add_format(book['id'],'EPUB',str(bilingual),replace=True,run_hooks=False) is False:
                    raise BridgeError('Calibre 拒絕主檔替換')
                if file_hash(self.api.format_abspath(book['id'],'EPUB'))!=manifest['sha256']['bilingual']:
                    raise BridgeError('雙語主檔 SHA 不符')
                self.api.set_field('identifiers',{book['id']:dict(original_ids,**{'ebook-translator-variant':'bilingual:'+run_id})})
                self.api.set_field('tags',{book['id']:list(dict.fromkeys(original_tags+['雙語_','附中譯_']))})
                # Publish the committed manifest last. Source backup remains on failure.
                self.add_data(book['id'],prefix+'manifest.json',json.dumps(manifest,ensure_ascii=False).encode(),replace=False)
            except Exception as original:
                errors=[]
                try:
                    if self.api.add_format(book['id'],'EPUB',str(backup),replace=True,run_hooks=False) is False:
                        raise BridgeError('Calibre 拒絕回復主檔')
                    if file_hash(self.api.format_abspath(book['id'],'EPUB'))!=book['facts']['sha256']:
                        raise BridgeError('回復後原檔 SHA 不符')
                except Exception as exc: errors.append(str(exc))
                for field,value in (('identifiers',original_ids),('tags',original_tags)):
                    try:self.api.set_field(field,{book['id']:value})
                    except Exception as exc:errors.append(str(exc))
                try:
                    committed=inside(book['directory'],prefix+'manifest.json')
                    if committed.exists():
                        self.api.remove_extra_files(book['id'],[prefix+'manifest.json'])
                        if committed.exists(): raise BridgeError('本次 manifest 回復移除失敗')
                except Exception as exc: errors.append(str(exc))
                if errors: raise BridgeError('封存回復未完成；Data 保留原檔 '+prefix+'source.epub；'+ '; '.join(errors)) from original
                raise

    def h2o_paths(self,book):
        return note_paths(self.library,self.h2o_root,book)

    def visible_plan(self,book,peer,pair_id):
        paths=self.h2o_paths(book)
        peer_paths=self.h2o_paths(peer)
        title=re.sub(r'[\r\n\[\]<>|]',' ',peer['title'])
        if paths:
            p=paths[0]
            before=p.read_bytes()
            text=before.decode('utf-8')
            matches=list(re.finditer(r'^## myNote[ \t]*\r?$',text,re.M))
            if len(matches)!=1: raise BridgeError('H2O 的 myNote 不唯一，未寫入')
            newline='\r\n' if b'\r\n' in before else '\n'
            link='[['+peer_paths[0].stem+']]' if peer_paths else '《'+title+'》'
            inserted=f'- 版本關係：對照版本 {link}（Calibre #{peer["id"]}） <!-- wasn-version-pair:{pair_id} -->'+newline
            pos=matches[0].end()
            # Keep existing newline and every original byte.
            pos=text.find('\n',pos)
            if pos<0: raise BridgeError('myNote 標題缺換行，未寫入')
            after=(text[:pos+1]+inserted+text[pos+1:]).encode('utf-8')
            return {'kind':'h2o','path':str(p),'before':before,'after':after,'inserted':inserted}
        before=str(self.api.field_for('comments',book['id']) or '').encode('utf-8')
        label='中文版' if not any(is_zh(v) for v in book['languages']) else '英文原書'
        prefix=f'<p data-wasn-version-pair="{pair_id}">版本關係：{label}《{html.escape(peer["title"])}》（Calibre #{peer["id"]}）</p>\n'
        return {'kind':'comments','before':before,'after':prefix.encode()+before,'inserted':prefix}

    def write_visible(self,book,plan,expected,new):
        if plan['kind']=='comments':
            current=str(self.api.field_for('comments',book['id']) or '').encode('utf-8')
            if current!=expected: raise BridgeError('簡介已被其他操作修改，未覆寫')
            self.api.set_field('comments',{book['id']:new.decode('utf-8')})
            if str(self.api.field_for('comments',book['id']) or '').encode('utf-8')!=new:
                raise BridgeError('Calibre 改寫了簡介原 HTML；需回復')
        else:
            path=Path(plan['path'])
            if path.read_bytes()!=expected: raise BridgeError('H2O 已變更，未覆寫')
            # Reuse ai_highlight GuardedWrite's reviewed S796 atomic primitive.
            # Only this exact myNote patch is supplied, never regenerate highlights.
            try: self.note_writer(path,expected,new)
            except Exception as exc: raise BridgeError('待核對：H2O 雜湊或寫入驗證不符，請核對外部編輯與版本關係行') from exc
            if path.read_bytes()!=new or digest(path.read_bytes())!=digest(new):
                raise BridgeError('H2O after_sha256 不符，請核對')

    def save_relations(self,book,value):
        value=dict(value,library_uuid=book['library_uuid'],book_uuid=book['uuid'])
        self.add_data(book['id'],RELFILE,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))

    def relation(self,book,peer,decision,expected_plans=None):
        pair_id=digest((book['library_uuid']+':'+':'.join(sorted([book['uuid'],peer['uuid']]))).encode())[:24]
        if expected_plans is not None:
            actual={'source':note_plan(self.library,self.h2o_root,book),'peer':note_plan(self.library,self.h2o_root,peer)}
            if actual!=expected_plans: raise BridgeError('待核對：版本關係落點或before_sha已變更，未寫入')
        plans=[]
        for current,other in ((book,peer),(peer,book)):
            old=current['relations']
            previous=[r for r in old['relations'] if r.get('pair_id')==pair_id]
            if previous:
                if previous[0]['decision']!=decision: raise BridgeError('已有不同決定，請先撤銷')
                continue
            visible=self.visible_plan(current,other,pair_id) if decision=='same' else None
            entry={'pair_id':pair_id,'library_uuid':book['library_uuid'],'peer_id':other['id'],'peer_uuid':other['uuid'],
                   'peer_title':other['title'],'decision':decision,'created_at':time.time()}
            if visible:
                entry.update(visible={k:v for k,v in visible.items() if k not in ('before','after')},
                             before_sha256=digest(visible['before']),after_sha256=digest(visible['after']))
            plans.append((current,old,dict(old,relations=old['relations']+[entry]),visible))
        applied=[]
        try:
            for current,old,new,visible in plans:
                applied.append((current,old,visible))
                if visible: self.write_visible(current,visible,visible['before'],visible['after'])
                self.save_relations(current,new)
        except Exception as original:
            errors=[]
            for current,old,visible in reversed(applied):
                try:
                    if visible:
                        actual=(str(self.api.field_for('comments',current['id']) or '').encode('utf-8')
                                if visible['kind']=='comments' else Path(visible['path']).read_bytes())
                        if actual==visible['after']:
                            self.write_visible(current,visible,visible['after'],visible['before'])
                        elif actual!=visible['before']:
                            raise BridgeError('回復遇到外部改動，保留現況，需人工核對')
                    self.save_relations(current,old)
                except Exception as exc: errors.append(str(exc))
            if errors: raise BridgeError('版本關係回復未完成：'+ '; '.join(errors)) from original
            raise

    def undo_relation(self,book,pair_id):
        records=[r for r in book['relations']['relations'] if r.get('pair_id')==pair_id]
        if len(records)!=1: raise BridgeError('找不到唯一的本側版本關係')
        rec=records[0]; visible=rec.get('visible')
        current=None; new=None; visible_removed=not visible
        if visible:
            if visible['kind']=='h2o':
                if self.h2o_root is None: raise BridgeError('H2O 路徑未設定')
                original=Path(visible['path']).resolve();original.relative_to(self.h2o_root)
                paths=note_paths(self.library,self.h2o_root,book,pattern='*.md')
                needle=visible['inserted'].encode('utf-8')
                marker=('wasn-version-pair:'+pair_id).encode()
                marked=[]
                own_sources={path.resolve() for path in paths}
                for candidate in self.h2o_root.rglob('*.md'):
                    resolved=candidate.resolve();resolved.relative_to(self.h2o_root)
                    raw=candidate.read_bytes()
                    # Both sides legitimately share pair_id but have distinct
                    # directional inserted lines and Source identities. Count
                    # our exact line anywhere, plus edited markers in our own
                    # Source note; never mistake the peer's line for our copy.
                    if marker in raw and (needle in raw or resolved in own_sources):marked.append(resolved)
                if len(marked)>1: raise BridgeError('待核對：工具版本行不唯一，未清理任何筆記或內部記錄')
                if marked:
                    p=marked[0]
                    if p not in [path.resolve() for path in paths]: raise BridgeError('待核對：工具行的 Source 已變更，未猜測筆記身分')
                    current=p.read_bytes()
                    if current.count(needle)!=1: raise BridgeError('待核對：工具版本行已由人工修改，未刪內部記錄')
                    visible=dict(visible,path=str(p))
                else:
                    if not original.is_file(): raise BridgeError('待核對：原筆記已移走或刪除，無法證明可見行已撤銷')
                    if original not in [path.resolve() for path in paths]: raise BridgeError('待核對：原筆記的 Source 已變更')
                    current=original.read_bytes()
            else: current=str(self.api.field_for('comments',book['id']) or '').encode('utf-8')
            needle=visible['inserted'].encode('utf-8')
            if current.count(needle)>1: raise BridgeError('工具版本關係行不唯一，未動其他內容')
            if current.count(needle)==1:
                new=current.replace(needle,b'',1)
                self.write_visible(book,visible,current,new);visible_removed=True
            else:
                # An edited HTML pair-id may remove the exact marker while
                # leaving the visible relationship row. Do not call it removed.
                plain=re.sub(rb'<!--.*?-->',b'',needle).strip()
                target=('Calibre #'+str(rec['peer_id'])).encode()
                if visible['kind']=='h2o' and (plain in current or any('版本關係'.encode() in line and target in line for line in current.splitlines())):
                    raise BridgeError('待核對：版本行仍在但工具標記已改，未刪內部記錄')
                visible=None  # Explicit Data-only result, never a clean-visible-row claim.
        try:
            self.save_relations(book,dict(book['relations'],relations=[r for r in book['relations']['relations'] if r.get('pair_id')!=pair_id]))
        except Exception:
            if visible: self.write_visible(book,visible,new,current)
            raise
        return visible_removed
