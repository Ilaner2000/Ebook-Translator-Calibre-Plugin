# S975 remote translation bridge

An opt-in local spool is consumed on the Calibre Qt main thread, then enters
ConversionWorker.translate_variants and the real GUI JobManager. No listener,
alternative provider, GUI launch or restart is introduced.

lib/remote_core.py is an exact embedded copy of Scripts' canonical
Scripts-ByAI/ebook_translation_core.py. The companion delivery checker must
compare both before packaging. Remote completion is emitted only after storing
variants succeeds. Device-confirmed reading-asset choices affect noninteractive
calls only; existing manual GUI calls retain their dialogs.

remote_bridge_root and remote_h2o_root default to unset. The deployment owner
must derive the local runtime spool and vault H2O paths from wasn_paths, not
copy a test worktree path. Installation and Calibre restart belong to the
foreman and must wait until no translation job is active.

The companion Scripts docs/s975/README.md records original dispatch, copied
real-library tests, actual queued GUI-job proof, fixtures and pending live /
provider / physical-device / AY acceptance. No full-book completion is claimed.


R2: canonical core byte equality is also preserved on fresh Windows checkout by the targeted .gitattributes LF rule. Durable per-request outcomes let Qt completion/running/storage events avoid contention with the API queue lock; next tick drains them. New ownership is needs_reconciliation, never automatic replay or proof of death. The authenticated explicit reconcile action requires no affected GUI jobs and either verified prior process exit or same-process job-list proof. A live/unknown other PID keeps the lock. Default bridge settings remain unset. Full findings and evidence: Scripts candidate docs/s975/README.md R2 section.
