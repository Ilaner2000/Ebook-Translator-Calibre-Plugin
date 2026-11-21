"""Qt main-thread adapter; no listener, subprocess launcher, or new engine."""
import copy
import os
import tempfile
from pathlib import Path
from qt.core import QApplication,QTimer
import importlib.util
from . import remote_core as core
from .config import get_config, Configuration
from .conversion import ConversionWorker
from .ebook import Ebook
from .translation import get_engine_class
from .utils import log

def gui_blocked(gui):
    if QApplication.activeModalWidget() is not None: return True
    windows=getattr(gui.bookfere_ebook_translator,'windows',{})
    if isinstance(windows,dict):
        return any(name=='batch' or name.startswith('advanced_') for name in windows)
    return False


def guarded_note_writer(module_path):
    if not module_path: raise core.BridgeError('尚未設定既有 H2O GuardedWrite primitive，未寫筆記')
    if not Path(module_path).is_absolute(): raise core.BridgeError('H2O guard 必須是明確的絕對路徑')
    path=Path(module_path).resolve(strict=True)
    if path.name!='migrate_h2o_kangxi_s796.py': raise core.BridgeError('H2O guard 必須指向既有受控 primitive')
    spec=importlib.util.spec_from_file_location('_wasn_s975_h2o_atomic',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.atomic_replace


class RemoteWorker(ConversionWorker):
    def __init__(self, gui, icon, done, *, request=None, book=None, root=None, backup_root=None):
        super().__init__(gui,icon)
        self.done=done
        self.remote_request=request;self.remote_book=book;self.bridge_root=Path(root) if root else None
        self.backup_root=Path(backup_root) if backup_root else None
        # Request-local output/notification settings; never mutate JSONConfig.
        self.config=Configuration(copy.deepcopy(dict(self.config.preferences)))
        self.config.update(to_library=True,translation_variants_enabled=True,show_notification=False)

    def preserve_conflict(self, payload, reason, library_written=False):
        req=self.remote_request
        if self.backup_root is None: raise core.BridgeError('來源衝突且未設定輸出保全根；請保留 Calibre 暫存工作檔')
        root=self.backup_root/req['id'];root.mkdir(parents=True,exist_ok=True)
        files={}
        for role,key in (('source','source_path'),('translation','translation_path'),('bilingual','bilingual_path')):
            source=Path(payload[key])
            if not source.is_file(): raise core.BridgeError('衝突輸出缺少 '+role+'；未寫書庫，請核對 Calibre 工作檔')
            data=source.read_bytes();expected=core.digest(data)
            dest=root/(role+'.epub')
            fd,tmp=tempfile.mkstemp(prefix='.'+role,dir=root)
            try:
                with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                os.replace(tmp,dest)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            if core.file_hash(dest)!=expected: raise core.BridgeError('衝突輸出保全 SHA 不符；未寫書庫')
            files[role]={'path':str(dest),'sha256':expected}
        receipt={'request':req,'source_evidence':req['source_evidence'],'reason':reason,
                 'files':files,'library_written':library_written,'resolved_at':None,'provider_result':'produced but not stored'}
        core.atomic_json(root/'manifest.json',receipt)
        return root

    def check_before_store(self, payload):
        if getattr(self,'remote_request',None) is None: raise core.BridgeError('遠端工作缺少原始確認證據，未存回')
        try:
            closed=getattr(self.api,'is_closed',False)
            if (closed() if callable(closed) else closed) is True: raise core.BridgeError('固定來源書庫 handle 已關閉，未寫目前書庫')
            current=core.snapshot(self.db.library_path,self.remote_request['book_id'])
            if self.api.field_for('uuid',self.remote_request['book_id'])!=self.remote_request['book_uuid']:
                raise core.BridgeError('固定來源書庫 handle 已失效或目標 UUID 已變更')
            if core.book_evidence(current)!=self.remote_request['source_evidence']:
                raise core.BridgeError('原書身分、題名、作者、語言或內文已變更')
            if not core.availability(current)['can_translate']:
                raise core.BridgeError('原書已不再是可翻譯的英文原書，可能另有譯本或版本記錄')
            if core.file_hash(payload['source_path'])!=self.remote_request['source_sha256']:
                raise core.BridgeError('工作來源副本 SHA 已變更')
        except Exception as exc:
            preserved=self.preserve_conflict(payload,str(exc))
            raise core.BridgeError('譯本未存回；來源需核對，三版本輸出已保存於 '+str(preserved)) from exc

    def translate_done(self,job):
        if gui_blocked(self.gui):
            if hasattr(self.done,'deferred'): self.done.deferred()
            QTimer.singleShot(1000,lambda:self.translate_done(job))
            return
        try:
            ebook,payload=self.working_jobs.pop(job)
            if job.failed: raise core.BridgeError('Calibre 翻譯 job 失敗；詳見 Calibre 工作紀錄')
            if not isinstance(payload,dict) or not payload.get('variants'):
                raise core.BridgeError('預期三版本輸出，未存回書庫')
            result=getattr(job,'result',None)
            if isinstance(result,dict):
                payload=dict(payload,v2_unrecoverable_keep_source=result.get('v2_unrecoverable_keep_source',False) is True,
                             kept_source_paragraphs=result.get('kept_source_paragraphs',[]))
            self.check_before_store(payload)
            if hasattr(self.done,'storing'): self.done.storing()
            try:
                self._store_variants(ebook,payload)
            except Exception as exc:
                preserved=self.preserve_conflict(payload,str(exc),library_written=None)
                raise core.BridgeError('存回未完成；請核對可能的部分寫入，輸出已保存於 '+str(preserved)) from exc
            self.done(True,'翻譯完成，版本已存回 Calibre')
        except Exception as exc:
            self.done(False,str(exc))

class RemoteBridge:
    def __init__(self,action):
        self.action=action
        self.consumer=None
        self.binding=None
        self.last_error=None

    def readiness(self):
        if gui_blocked(self.action.gui): return False,'請先完成 Calibre 對話框或手動翻譯流程'
        if not get_config().get('remote_conflict_root'): return False,'尚未設定三版本衝突備份根'
        engine=get_engine_class()
        target=engine.config.get('target_lang')
        try:
            code=engine.get_iso639_target_code(target)
        except (KeyError,TypeError,ValueError):
            code=''
        if not (core.is_zh(code) or any(s in str(target).lower() for s in ('chinese','中文','繁體','简体','簡體'))): return False,'請先在翻譯外掛設定中文目標語言'
        return True,''

    def manual_jobs(self):
        values=[]
        for job,info in self.action.gui.bookfere_ebook_translator.jobs.items():
            if info[0].id is None: continue
            identity=getattr(job,'wasn_source_identity',None)
            values.append(identity if isinstance(identity,dict) else {'known':False,'book_id':int(info[0].id)})
        return values

    def start(self,request,book,done):
        # This runs on Qt, bound to the current library and exact source bytes.
        engine=get_engine_class()
        target=engine.config.get('target_lang')
        code=engine.get_iso639_target_code(target)
        if not core.is_zh(code) and any(s in str(target).lower() for s in ('chinese','中文','繁體','简体','簡體')): code='zh'
        if not core.is_zh(code): raise core.BridgeError('目標語言已變更；未啟動')
        source_codes=engine.lang_codes.get('source') or {}
        source=next((name for name,value in source_codes.items() if str(value).lower() in ('en','eng','english') or str(name).lower() in ('english','英文')),None)
        if source is None: raise core.BridgeError('目前引擎不支援英文來源語言')
        ebook=Ebook(book['id'],book['title'],{'epub':book['epub']},'epub',source)
        ebook.set_output_format('epub'); ebook.set_target_lang(target); ebook.set_lang_code(code)
        worker=RemoteWorker(self.action.gui,self.action.icon,done,request=request,book=book,root=done.bridge_root,backup_root=get_config().get('remote_conflict_root'))
        if request['reading_policy']=='linked_pair':
            worker.config.set('translation_variant_storage','linked_pair')
        return worker.translate_variants(ebook,book['epub'],is_batch=True,
                                         reading_policy=request['reading_policy'],interactive=False,expected_source_sha256=request['source_sha256'])

    def tick(self):
        config=get_config()
        root=config.get('remote_bridge_root')
        if not root: return  # Installation alone never enables a bridge.
        try:
            root=Path(root)
            if 'runtime-tmp' in root.parts: raise core.BridgeError('在飛紀錄不能放在收工會清空的 runtime-tmp')
            if not root.is_absolute(): raise core.BridgeError('remote_bridge_root 必須是絕對路徑')
            backup=Path(config.get('remote_conflict_root') or '')
            if not backup.is_absolute() or 'runtime-tmp' in backup.parts: raise core.BridgeError('衝突輸出必須設定到獨立的備份根')
            api=self.action.gui.current_db.new_api
            library=Path(self.action.gui.current_db.library_path).resolve()
            binding=(str(root.resolve()),str(library))
            if binding!=self.binding:
                self.consumer=core.Consumer(root,library,api,self.start,
                    h2o_root=config.get('remote_h2o_root'),readiness=self.readiness,manual_jobs=self.manual_jobs,
                    note_writer=guarded_note_writer(config.get('remote_note_guard_path')),backup_root=config.get('remote_conflict_root'))
                self.binding=binding
            if gui_blocked(self.action.gui):
                self.consumer.heartbeat()
                return
            self.consumer.tick()
            self.last_error=None
        except Exception as exc:
            if str(exc)!=self.last_error:
                log.warn('Remote translation bridge:',str(exc))
                self.last_error=str(exc)
