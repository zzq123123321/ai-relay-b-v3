-- AI Relay B V3.0：初始权威结构 schema v1（T04）。
-- 本脚本为纯 DDL+DML，不含 PRAGMA / BEGIN / COMMIT：
--  连接设置与事务边界由 storage/schema.py 迁移器统一控制（显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK）。
-- 依据：附录 schema_v3.sql 草案，保留全部核心权威表、索引与触发器。

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version','1'),('next_sequence','1');

CREATE TABLE config_revisions (
  revision INTEGER PRIMARY KEY, config_json TEXT NOT NULL, sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL, actor TEXT NOT NULL);

CREATE TABLE project_bindings (
  project_key TEXT PRIMARY KEY, display_path TEXT NOT NULL, endpoint TEXT NOT NULL,
  session_id TEXT, binding_revision INTEGER NOT NULL DEFAULT 0,
  rotation_count INTEGER NOT NULL DEFAULT 0 CHECK(rotation_count>=0),
  rotation_sequence INTEGER NOT NULL DEFAULT 0 CHECK(rotation_sequence>=0),
  rotation_base_title TEXT NOT NULL DEFAULT '', candidate_operation_id TEXT,
  config_revision INTEGER REFERENCES config_revisions(revision));

CREATE TABLE tasks (
  task_key TEXT PRIMARY KEY, peer_id TEXT NOT NULL, task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL UNIQUE CHECK(sequence>0),
  protocol_format TEXT NOT NULL CHECK(protocol_format IN ('V1','LEGACY_WEB','V2')),
  raw_message TEXT NOT NULL, body TEXT NOT NULL, canonical_hash TEXT NOT NULL,
  received_at TEXT NOT NULL, ingress_snapshot_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('QUEUED','ACTIVE','BLOCKED','COMPLETED','FAILED','STOPPED_BY_USER')),
  blocked_reason TEXT, active_attempt_id TEXT,
  authority_epoch INTEGER NOT NULL DEFAULT 0 CHECK(authority_epoch>=0),
  current_result_revision INTEGER NOT NULL DEFAULT 0 CHECK(current_result_revision>=0),
  UNIQUE(peer_id,task_id));

CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY, task_key TEXT NOT NULL REFERENCES tasks(task_key),
  parent_attempt_id TEXT REFERENCES attempts(attempt_id),
  kind TEXT NOT NULL CHECK(kind IN ('INITIAL','MANUAL_CONTINUE','NEW_SESSION_RETRY','MANUAL_RESOLUTION')),
  state TEXT NOT NULL CHECK(state IN ('OPEN','COMPLETED','FAILED','STOPPED','SUPERSEDED')),
  authority_epoch INTEGER NOT NULL CHECK(authority_epoch>=0),
  execution_snapshot_json TEXT NOT NULL, control_revision INTEGER NOT NULL DEFAULT 0,
  remote_state TEXT NOT NULL DEFAULT 'NOT_SENT'
    CHECK(remote_state IN ('NOT_SENT','BUSY','RETRY','IDLE_VERIFIED','WAITING_USER','MAYBE_RUNNING','UNKNOWN')),
  started_at TEXT NOT NULL, ended_at TEXT, remote_summary_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(task_key,attempt_id));
CREATE UNIQUE INDEX one_open_attempt ON attempts(task_key) WHERE state='OPEN';

CREATE TABLE project_leases (
  project_key TEXT PRIMARY KEY, owner_task_key TEXT NOT NULL REFERENCES tasks(task_key),
  owner_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), authority_epoch INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','QUARANTINED','ROTATING')),
  related_sessions_json TEXT NOT NULL DEFAULT '[]', last_verified_at TEXT, reason TEXT,
  FOREIGN KEY(owner_task_key,owner_attempt_id) REFERENCES attempts(task_key,attempt_id));

CREATE TABLE operations (
  operation_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('INITIAL_SEND','CONTINUE','CREATE_SESSION','SET_PERMISSION')),
  task_key TEXT REFERENCES tasks(task_key), attempt_id TEXT REFERENCES attempts(attempt_id),
  authority_epoch INTEGER, control_revision INTEGER, endpoint TEXT NOT NULL,
  session_id TEXT, project_key TEXT, interruption_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('PREPARED','SENDING','ACCEPTED','REJECTED','UNKNOWN','CANCELLED')),
  pre_snapshot_json TEXT NOT NULL DEFAULT '{}', prompt_hash TEXT, prompt_text TEXT,
  remote_user_id TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finalized_at TEXT);
CREATE UNIQUE INDEX one_unresolved_send_per_session
  ON operations(endpoint,session_id)
  WHERE kind IN ('INITIAL_SEND','CONTINUE') AND state IN ('SENDING','UNKNOWN') AND session_id IS NOT NULL;

CREATE TABLE interruptions (
  interruption_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  fingerprint TEXT NOT NULL, reason TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, observation_count INTEGER NOT NULL DEFAULT 1 CHECK(observation_count>=1),
  state TEXT NOT NULL CHECK(state IN ('OPEN','RESOLVED','BLOCKED')),
  last_operation_id TEXT REFERENCES operations(operation_id));
CREATE UNIQUE INDEX one_open_fingerprint ON interruptions(attempt_id,fingerprint) WHERE state='OPEN';

CREATE TABLE recovery_runtime (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  phase TEXT NOT NULL CHECK(phase IN ('WATCHING','WAIT_NETWORK','VERIFYING','SUSPECTED','SCHEDULED','SENDING','AWAITING_PROGRESS','COOLDOWN','PAUSED','BLOCKED','NONE')),
  pending_operation_id TEXT REFERENCES operations(operation_id),
  interruption_id TEXT REFERENCES interruptions(interruption_id),
  resume_total INTEGER NOT NULL DEFAULT 0 CHECK(resume_total>=0),
  resume_send_attempts INTEGER NOT NULL DEFAULT 0 CHECK(resume_send_attempts>=0),
  consecutive_no_progress INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_no_progress>=0),
  next_check_at TEXT, last_real_progress_at TEXT, last_progress_signature TEXT,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), resume_after_restart INTEGER NOT NULL CHECK(resume_after_restart IN (0,1)),
  runtime_json TEXT NOT NULL DEFAULT '{}');

CREATE TABLE observations (
  observation_id TEXT PRIMARY KEY, task_key TEXT REFERENCES tasks(task_key),
  attempt_id TEXT REFERENCES attempts(attempt_id), target_key TEXT NOT NULL,
  target_generation INTEGER NOT NULL DEFAULT 0, request_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
  summary_json TEXT NOT NULL, attribution TEXT NOT NULL);

CREATE TABLE results (
  result_id TEXT PRIMARY KEY, task_key TEXT NOT NULL REFERENCES tasks(task_key),
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), revision INTEGER NOT NULL CHECK(revision>0),
  state TEXT NOT NULL CHECK(state IN ('COMPLETED','FAILED','STOPPED_BY_USER')),
  source TEXT NOT NULL CHECK(source IN ('AUTO_RELAY','MANUAL_WRAP','STARTUP_RECONCILE','LOCAL_REJECTION','USER_STOP','MANUAL_MONITOR')),
  final_body TEXT NOT NULL, protocol_text TEXT NOT NULL, sha256 TEXT NOT NULL,
  remote_message_ids_json TEXT NOT NULL DEFAULT '[]', committed_at TEXT NOT NULL,
  UNIQUE(task_key,revision),
  FOREIGN KEY(task_key,attempt_id) REFERENCES attempts(task_key,attempt_id));

CREATE TABLE outbox (
  delivery_id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES results(result_id), peer_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','OFFERED','ACKED','MANUAL_CONFIRMED','BLOCKED','ARCHIVED')),
  profile TEXT NOT NULL CHECK(profile IN ('legacy_v1','reliable_v2')),
  offered_at TEXT, acked_at TEXT, offered_count INTEGER NOT NULL DEFAULT 0 CHECK(offered_count>=0),
  last_error TEXT, UNIQUE(result_id,peer_id));

CREATE TABLE remote_claims (
  endpoint TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
  task_key TEXT NOT NULL REFERENCES tasks(task_key), result_id TEXT NOT NULL REFERENCES results(result_id),
  PRIMARY KEY(endpoint,session_id,message_id));

CREATE TABLE monitor_bindings (
  monitor_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, project_key TEXT NOT NULL, session_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0, completed_baseline_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), independent_owner_task_key TEXT REFERENCES tasks(task_key),
  config_json TEXT NOT NULL DEFAULT '{}', UNIQUE(endpoint,session_id));

CREATE TABLE events (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
  ts_utc TEXT NOT NULL, level TEXT NOT NULL CHECK(level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
  event_code TEXT NOT NULL, task_key TEXT, attempt_id TEXT, operation_id TEXT, result_id TEXT,
  session_id TEXT, summary_zh TEXT NOT NULL, fields_redacted_json TEXT NOT NULL DEFAULT '{}',
  critical_audit INTEGER NOT NULL DEFAULT 0 CHECK(critical_audit IN (0,1)),
  aggregation_count INTEGER NOT NULL DEFAULT 1 CHECK(aggregation_count>=1));

CREATE TABLE ui_prefs (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE exchanges (
  peer_id TEXT NOT NULL, exchange_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  response_text TEXT NOT NULL, result_id TEXT REFERENCES results(result_id),
  state TEXT NOT NULL CHECK(state IN ('CACHED','ACKED','EXPIRED')),
  created_at TEXT NOT NULL, expires_at TEXT, PRIMARY KEY(peer_id,exchange_id));

CREATE INDEX tasks_fifo ON tasks(state,sequence);
CREATE INDEX events_task_time ON events(task_key,ts_utc,event_seq);
CREATE INDEX events_code_time ON events(event_code,ts_utc,event_seq);
CREATE INDEX operations_attempt ON operations(attempt_id,created_at);
CREATE INDEX observations_target_time ON observations(target_key,observed_at);
CREATE INDEX outbox_pending ON outbox(peer_id,state,delivery_id);

CREATE TRIGGER config_no_update BEFORE UPDATE ON config_revisions BEGIN
  SELECT RAISE(ABORT,'immutable config revision'); END;
CREATE TRIGGER config_no_delete BEFORE DELETE ON config_revisions BEGIN
  SELECT RAISE(ABORT,'configuration archive requires explicit migration'); END;
CREATE TRIGGER results_no_update BEFORE UPDATE ON results BEGIN
  SELECT RAISE(ABORT,'immutable result'); END;
CREATE TRIGGER results_no_delete BEFORE DELETE ON results BEGIN
  SELECT RAISE(ABORT,'result archive requires explicit migration'); END;