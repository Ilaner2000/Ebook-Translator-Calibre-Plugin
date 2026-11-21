"""Qt main-thread adapter; no listener, subprocess launcher, or new engine."""
import copy
from pathlib import Path
from . import remote_core as core
from .config import get_config, Configuration
from .conversion import ConversionWorker
from .ebook import Ebook
from .translation import get_engine_class
from .utils import log

class RemoteWorker(ConversionWorker):
    def __init__(self, gui, icon, done):
        super().__init__(gui,icon)
        self.done=done
        # Request-local output/notification settings; never mutate JSONConfig.
        self.config=Configuration(copy.deepcopy(dict(self.config.preferences)))
        self.config.update(to_library=True,translation_variants_enabled=True,show_notification=False)

    def translate_done(self,job):
        try:
            ebook,payload=self.working_jobs.pop(job)
            if job.failed: raise core.BridgeError('Calibre 翻譯 job 失敗；詳見 Calibre 工作紀錄')
            if not isinstance(payload,dict) or not payload.get('variants'):
                raise core.BridgeError('預期三版本輸出，未存回書庫')
            result=getattr(job,'result',None)
            if isinstance(result,dict):
                payload=dict(payload,v2_unrecoverable_keep_source=result.get('v2_unrecoverable_keep_source',False) is True,
                             kept_source_paragraphs=result.get('kept_source_paragraphs',[]))
            self._store_variants(ebook,payload)
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
        engine=get_engine_class()
        target=engine.config.get('target_lang')
        try:
            code=engine.get_iso639_target_code(target)
        except (KeyError,TypeError,ValueError):
            code=''
        if not (core.is_zh(code) or any(s in str(target).lower() for s in ('chinese','中文','繁體','简体','簡體'))): return False,'請先在翻譯外掛設定中文目標語言'
        return True,''

    def manual_jobs(self):
        return sorted({int(info[0].id) for info in self.action.gui.bookfere_ebook_translator.jobs.values() if info[0].id is not None})

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
        worker=RemoteWorker(self.action.gui,self.action.icon,done)
        if request['reading_policy']=='linked_pair':
            worker.config.set('translation_variant_storage','linked_pair')
        return worker.translate_variants(ebook,book['epub'],is_batch=True,
                                         reading_policy=request['reading_policy'],interactive=False)

    def tick(self):
        config=get_config()
        root=config.get('remote_bridge_root')
        if not root: return  # Installation alone never enables a bridge.
        try:
            root=Path(root)
            if not root.is_absolute(): raise core.BridgeError('remote_bridge_root 必須是絕對路徑')
            api=self.action.gui.current_db.new_api
            library=Path(self.action.gui.current_db.library_path).resolve()
            binding=(str(root.resolve()),str(library))
            if binding!=self.binding:
                self.consumer=core.Consumer(root,library,api,self.start,
                    h2o_root=config.get('remote_h2o_root'),readiness=self.readiness,manual_jobs=self.manual_jobs)
                self.binding=binding
            self.consumer.tick()
            self.last_error=None
        except Exception as exc:
            if str(exc)!=self.last_error:
                log.warn('Remote translation bridge:',str(exc))
                self.last_error=str(exc)
