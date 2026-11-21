"""S975 shared book inspection and GUI-bridge protocol (stdlib only).

Canonical source: Scripts-ByAI/ebook_translation_core.py. The plugin ships an
identical copy; delivery verification must compare their SHA-256 values.
"""
from __future__ import annotations
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET
from html.parser import HTMLParser

SCHEMA = 1
ACTIVE = {'queued', 'starting', 'running', 'storing'}
TERMINAL = {'completed', 'failed', 'rejected'}
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

def snapshot(library, book_id):
    root = Path(library).resolve(strict=True)
    with sqlite3.connect((root/'metadata.db').as_uri()+'?mode=ro', uri=True) as db:
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
    results=[]
    directory = Path(book['directory'])
    manifests=list((directory/'data'/'translation-variants').glob('*/manifest.json'))
    # Older linked_pair layouts keep the manifest and bilingual attachment on
    # the translated record, linked by the original's UUID (not title guessing).
    main_paths={book['id']:book.get('epub')}
    library=Path(book['library_path'])
    with sqlite3.connect((library/'metadata.db').as_uri()+'?mode=ro',uri=True) as db:
        related=db.execute("SELECT DISTINCT b.id,b.path FROM books b JOIN identifiers i ON i.book=b.id WHERE i.type='ebook-translator-source' AND i.val=?",(book['uuid'],)).fetchall()
        for bid,relative in related:
            other=inside(library,relative)
            manifests.extend((other/'data'/'translation-variants').glob('*/manifest.json'))
            fmt=db.execute("SELECT name FROM data WHERE book=? AND upper(format)='EPUB'",(bid,)).fetchall()
            if len(fmt)==1: main_paths[bid]=str(inside(other,fmt[0][0]+'.epub'))
    for p in sorted(set(manifests)):
        p.resolve().relative_to(library)
        m = read_json(p)
        if not isinstance(m,dict):
            raise BridgeError('翻譯 manifest 無法讀取')
        if str(m.get('source_uuid')) != book['uuid'] and book['id'] not in (m.get('derivative_book_ids') or {}).values():
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
            results.append({'manifest':m,'manifest_path':str(p),'roles':roles})
        else:
            raise BridgeError('翻譯紀錄存在，但中譯／雙語檔缺失或 SHA 不符')
    return results

def availability(book):
    facts=book.get('facts')
    base={'book_id':book['id'],'book_uuid':book['uuid'],'library_uuid':book['library_uuid'],
          'title':book['title'],'can_translate':False,'can_archive':False}
    def result(state, reason, **more):
        return dict(base,state=state,reason=reason,**more)
    confirmed=[r for r in book['relations']['relations'] if r.get('decision')=='same' and r.get('library_uuid')==book['library_uuid']]
    if confirmed:
        return result('translated','已確認中文版：'+ '、'.join(f"{r['peer_title']}（#{r['peer_id']}）" for r in confirmed))
    variants=verified_variants(book)
    if facts:
        archive=[v for v in variants if 'bilingual' in v['roles'] and v['manifest']['sha256'].get('source')==facts['sha256']]
        if len(archive)==1:
            v=archive[0]
            return result('archive','英文原檔仍為主檔；已有可驗證雙語版，可整理原檔',can_archive=True,
                          source_sha256=facts['sha256'],archive=v)
        if variants:
            return result('translated','已有可驗證的中譯／雙語 EPUB',source_sha256=facts['sha256'])
    tags=set(book['tags'])
    marked=tags.intersection({'中譯_','雙語_','附雙語_','附中譯_'}) or any(k in book['identifiers'] for k in ('ebook-translator-variant','ebook-translator-source'))
    if marked:
        return result('translated','已有中譯／雙語標記，停止重複翻譯（舊書附件仍需人工核對）')
    langs=book['languages']+(facts['languages'] if facts else [])
    if any(is_zh(v) for v in langs) or (facts and facts['han']>=50 and facts['han_ratio']>.2):
        return result('chinese','中文書，無需翻譯')
    if not facts:
        return result('unsupported','目前整本翻譯入口支援 EPUB；本書無 EPUB')
    if facts['han']>50 and facts['han_ratio']>.015:
        return result('uncertain','內文含可觀中文，可能已有譯文，請先核對')
    if facts['latin']<200 or not any(lang(v) in ('en','eng','en-us','en-gb') for v in langs):
        return result('uncertain','無法確認為英文原書，請先核對語言')
    return result('eligible','英文原書，尚未找到本書譯本',can_translate=True,source_sha256=facts['sha256'])

def candidates(library, book):
    """Suggestions only: even an exact source UUID is shown for confirmation."""
    omitted={r.get('peer_uuid') for r in book['relations']['relations'] if r.get('decision') in ('different','same')}
    normalized=lambda s: re.sub(r'[^\w]','',s.lower())
    title=normalized(book['title'])
    authors={normalized(a) for a in book['authors']}
    out=[]
    root=Path(library)
    with sqlite3.connect((root/'metadata.db').as_uri()+'?mode=ro',uri=True) as db:
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
    return sorted(out,key=lambda r:(-len(r['reasons']),r['id']))[:12]

def public_state(library, book_id):
    try:
        book=snapshot(library,book_id)
        state=availability(book)
        archived=state.pop('archive',None)
        if archived:
            state['archive_preview']={'source':book['epub'],'source_sha256':state['source_sha256'],
                'destination':'data/translation-variants/<本次新建目錄>/source.epub',
                'new_main':archived['roles']['bilingual'],
                'new_main_sha256':archived['manifest']['sha256']['bilingual']}
        state['candidates']=candidates(library,book) if state['state']=='eligible' else []
        state['relations']=book['relations']['relations']
        return state
    except (OSError,ValueError,KeyError,RuntimeError,sqlite3.Error,zipfile.BadZipFile,ET.ParseError) as exc:
        return {'book_id':book_id,'state':'uncertain','can_translate':False,'can_archive':False,'reason':str(exc)}

def bridge_health(root, book):
    hb=read_json(Path(root)/'heartbeat.json',{})
    valid=(hb.get('schema')==SCHEMA and hb.get('library_uuid')==book.get('library_uuid') and
           time.time()-float(hb.get('at',0)) < 15 and hb.get('ready') is True)
    return {'ready':valid,'reason':hb.get('reason','翻譯外掛橋接尚未上線') if not valid else '',
            'instance':hb.get('instance'), 'manual_jobs':hb.get('manual_jobs',[])}

def requests(root):
    for p in sorted((Path(root)/'requests').glob('*.json')):
        req=read_json(p)
        if not isinstance(req,dict) or req.get('schema')!=SCHEMA:
            raise BridgeError('橋接佇列資料損壞')
        yield p,req

def enqueue(root, library, book_id, operation, *, source_sha256=None, reading_policy=None, peer_id=None, decision=None, request_id=None, pair_id=None):
    if operation not in ('translate','archive','relation','undo_relation'):
        raise BridgeError('未知操作')
    with serial(root):
        book=snapshot(library,book_id)
        state=availability(book)
        health=bridge_health(root,book)
        if not health['ready']: raise BridgeError(health['reason'])
        if book_id in health['manual_jobs']: raise BridgeError('這本書已在 Calibre 翻譯中')
        if operation=='translate' and not state['can_translate']: raise BridgeError(state['reason'])
        if operation=='archive' and not state['can_archive']: raise BridgeError(state['reason'])
        if operation in ('translate','archive') and source_sha256 != state.get('source_sha256'):
            raise BridgeError('原書已變更，請重新開啟詳細頁')
        if operation=='translate' and reading_policy not in ('linked_pair','replace_main'):
            raise BridgeError('請先選擇閱讀資產的保留方式')
        peer=None
        if operation=='relation':
            if decision not in ('same','different') or int(peer_id)==int(book_id): raise BridgeError('版本配對選項無效')
            peer=snapshot(library,peer_id)
            if decision=='same' and not any(is_zh(v) for v in peer['languages']): raise BridgeError('對照書未標為中文，請先核對')
        active_ids={int(book_id)} | ({int(peer_id)} if peer else set())
        rid=request_id or uuid.uuid4().hex
        if not re.fullmatch('[a-f0-9]{32}',rid): raise BridgeError('request ID 無效')
        for _,old in requests(root):
            if old['id']==rid:
                if (old['book_uuid']!=book['uuid'] or old['operation']!=operation or
                    any(old.get(k)!=v for k,v in {'source_sha256':source_sha256,'reading_policy':reading_policy,'peer_id':peer_id,'decision':decision,'pair_id':pair_id}.items())):
                    raise BridgeError('request ID 衝突')
                return old
            if old.get('state') in ACTIVE and active_ids.intersection(set(old.get('book_ids',[old['book_id']]))):
                if old['operation']==operation and old['book_id']==book_id and operation in ('translate','archive'):
                    return old
                raise BridgeError('本書已有待處理操作，請等候結果')
        req={'schema':SCHEMA,'id':rid,'operation':operation,'state':'queued','created_at':time.time(),
             'expires_at':time.time()+120,'instance':health['instance'],'library_uuid':book['library_uuid'],
             'library_path':str(Path(library).resolve()),'book_id':int(book_id),'book_uuid':book['uuid'],
             'book_ids':sorted(active_ids),'source_sha256':source_sha256,'reading_policy':reading_policy,
             'peer_id':peer_id,'peer_uuid':peer['uuid'] if peer else None,'decision':decision,'pair_id':pair_id}
        atomic_json(Path(root)/'requests'/(rid+'.json'),req)
        return req

class Consumer:
    """Called only on the Calibre Qt main thread; API process only enqueues."""
    def __init__(self, root, library, api, start_translation, *, h2o_root=None, readiness=None, manual_jobs=None):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.library=Path(library).resolve(); self.api=api
        self.start_translation=start_translation; self.h2o_root=Path(h2o_root).resolve() if h2o_root else None
        self.readiness=readiness or (lambda: (True,''))
        self.manual_jobs=manual_jobs or (lambda: [])
        self.instance=uuid.uuid4().hex
        with sqlite3.connect((self.library/'metadata.db').as_uri()+'?mode=ro',uri=True) as db:
            self.library_uuid=str(db.execute('SELECT uuid FROM library_id').fetchone()[0])
        self.last_heartbeat=0

    def heartbeat(self):
        ready,reason=self.readiness()
        atomic_json(self.root/'heartbeat.json',{'schema':SCHEMA,'library_uuid':self.library_uuid,'at':time.time(),
                    'instance':self.instance,'pid':os.getpid(),'ready':ready,'reason':reason,'manual_jobs':self.manual_jobs()})

    def update(self, req, state, **fields):
        req.update(state=state,updated_at=time.time(),**fields)
        atomic_json(self.root/'requests'/(req['id']+'.json'),req)

    def finish(self, req, success, detail=''):
        with serial(self.root):
            self.update(req,'completed' if success else 'failed',detail=detail)

    def tick(self):
        self.heartbeat()
        with serial(self.root):
            pending=next(((p,r) for p,r in requests(self.root) if r.get('state')=='queued'),None)
            if pending is None: return
            _,req=pending
            if req['instance']!=self.instance or time.time()>req['expires_at']:
                self.update(req,'rejected',detail='外掛世代已變更或要求已逾期；未啟動')
                return
            self.update(req,'starting')
        try:
            book=snapshot(self.library,req['book_id'])
            if (book['library_uuid']!=req['library_uuid'] or book['uuid']!=req['book_uuid'] or
                str(self.library)!=req['library_path']):
                raise BridgeError('目前書庫或書籍身分與要求不符')
            if req['book_id'] in self.manual_jobs(): raise BridgeError('Calibre 已有同書翻譯 job')
            ready,reason=self.readiness()
            if not ready: raise BridgeError(reason)
            op=req['operation']
            if op in ('translate','archive'):
                st=availability(book)
                if req['source_sha256']!=st.get('source_sha256'): raise BridgeError('主檔已變更')
                if not st['can_'+op]: raise BridgeError(st['reason'])
            if op=='translate':
                job=self.start_translation(req,book,lambda success,detail='':self.finish(req,success,detail))
                if job is None: raise BridgeError('外掛未建立 GUI job')
                # A synchronous test callback may already have completed.
                if req['state'] not in TERMINAL:
                    with serial(self.root): self.update(req,'running',job_id=str(getattr(job,'id','')),detail='Calibre GUI job 已建立')
            elif op=='archive':
                self.archive(book,st['archive'])
                self.finish(req,True,'英文原檔已收進 Data，雙語版已成主 EPUB')
            elif op=='relation':
                peer=snapshot(self.library,req['peer_id'])
                if peer['uuid']!=req['peer_uuid']: raise BridgeError('對照書已變更')
                self.relation(book,peer,req['decision'])
                self.finish(req,True,'兩側版本關係已記錄，可各自撤銷')
            elif op=='undo_relation':
                self.undo_relation(book,req.get('pair_id'))
                self.finish(req,True,'本側版本關係已撤銷，其他內容保留')
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

    def archive(self,book,variant):
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
            self.add_data(book['id'],prefix+'manifest.json',json.dumps(manifest,ensure_ascii=False).encode(),replace=False)
            try:
                if self.api.add_format(book['id'],'EPUB',str(bilingual),replace=True,run_hooks=False) is False:
                    raise BridgeError('Calibre 拒絕主檔替換')
                if file_hash(self.api.format_abspath(book['id'],'EPUB'))!=manifest['sha256']['bilingual']:
                    raise BridgeError('雙語主檔 SHA 不符')
                self.api.set_field('identifiers',{book['id']:dict(original_ids,**{'ebook-translator-variant':'bilingual:'+run_id})})
                self.api.set_field('tags',{book['id']:list(dict.fromkeys(original_tags+['雙語_','附中譯_']))})
            except Exception:
                if self.api.add_format(book['id'],'EPUB',str(backup),replace=True,run_hooks=False) is False:
                    raise BridgeError('替換失敗且回復失敗；Data 已保留原檔 '+prefix+'source.epub')
                self.api.set_field('identifiers',{book['id']:original_ids})
                self.api.set_field('tags',{book['id']:original_tags})
                if file_hash(self.api.format_abspath(book['id'],'EPUB'))!=book['facts']['sha256']:
                    raise BridgeError('回復後原檔 SHA 不符；Data 仍保留備份')
                raise

    def h2o_paths(self,book):
        if self.h2o_root is None: raise BridgeError('尚未設定 H2O 目錄，不能決定版本關係落點')
        if not self.h2o_root.is_dir(): raise BridgeError('H2O 目錄無法存取')
        pattern=re.compile(r'^[ \t]*-[ \t]*Source[ \t]*::.*?calibre://(?:show|view)-book/([^/)]+)/(\d+)',re.I|re.M)
        out=[]
        for p in self.h2o_root.glob('R.*.md'):
            p.resolve().relative_to(self.h2o_root)
            m=pattern.search(p.read_text(encoding='utf-8'))
            if m and int(m.group(2))==book['id']:
                library_name=m.group(1)
                if library_name.startswith('_hex_-'):
                    library_name=bytes.fromhex(library_name[6:]).decode('utf-8')
                if library_name!=self.library.name:
                    raise BridgeError('H2O Source 指向另一個書庫，未寫入')
                out.append(p)
        if len(out)>1: raise BridgeError('本書有多份 H2O；請先指定版本，不猜落點')
        return out

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
            # Exclusive OS lock serialises this bridge's writes; compare-before
            # atomic-replace refuses changed text instead of replaying a backup.
            fd,tmp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as f: f.write(new); f.flush(); os.fsync(f.fileno())
                if path.read_bytes()!=expected: raise BridgeError('H2O 寫入前已變更')
                os.replace(tmp,path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

    def save_relations(self,book,value):
        value=dict(value,library_uuid=book['library_uuid'],book_uuid=book['uuid'])
        self.add_data(book['id'],RELFILE,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))

    def relation(self,book,peer,decision):
        pair_id=digest((book['library_uuid']+':'+':'.join(sorted([book['uuid'],peer['uuid']]))).encode())[:24]
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
        current=None; new=None
        if visible:
            if visible['kind']=='h2o':
                p=Path(visible['path']).resolve()
                if self.h2o_root is None: raise BridgeError('H2O 路徑未設定')
                p.relative_to(self.h2o_root)
                current=p.read_bytes()
            else: current=str(self.api.field_for('comments',book['id']) or '').encode('utf-8')
            needle=visible['inserted'].encode('utf-8')
            if current.count(needle)!=1: raise BridgeError('工具版本關係行已變更，未動其他內容')
            new=current.replace(needle,b'',1)
            self.write_visible(book,visible,current,new)
        try:
            self.save_relations(book,dict(book['relations'],relations=[r for r in book['relations']['relations'] if r.get('pair_id')!=pair_id]))
        except Exception:
            if visible: self.write_visible(book,visible,new,current)
            raise
