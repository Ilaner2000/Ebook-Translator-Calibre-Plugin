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


R3 lifecycle contract (T1370:m7): remote_bridge_root derives from wasn_paths.SCRATCHPAD_ROOT / ebook-translator-bridge; runtime-tmp refused. remote_conflict_root derives from WASN_TOOLS_BACKUPS_ROOT / ebook-translator-conflicts; only persistent backup root accepted. remote_h2o_root is vault/05-H2O. remote_note_guard_path is absolute path of existing Scripts-ByAI/migrate_h2o_kangxi_s796.py, the atomic primitive used by ai_highlight GuardedWrite. No Obsidian CLI. Same before/after bytes/hash and Windows creation-time rules; only explicit version-line patch, no regeneration of highlights. Source URL legacy ID/name identity limit is shown in preview; note path/beforeSHA are confirmed end-to-end.

Both manual/remote jobs bind fixed worker DB libraryUUID/bookUUID. Remote provider input is verified snapshot, before-store validates original source/handle; closed handle or source conflict retains three outputs under backup root with hashes. Modal metadata editor and advanced/batch pre-job windows pause consumer and remote completion. Durable outcome is independent of queue lock. Active/unknown never TTL; terminal history90d, resolved backup>=30d, unresolved indefinite; only bridge maintenance owns deletion. No automatic retry or conflict-resolution inference. See Scripts docs/s975/README.md R3 for complete evidence, fixture limitations and activation checklist; installation/restart is still foreman-owned after no-active-job check and dual AGREE.
