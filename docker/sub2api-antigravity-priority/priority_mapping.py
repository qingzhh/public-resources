"""Versioned native priority translation. All account data stays on this host."""
import json
import os
import pathlib
import tempfile

OFFSET = 1000
SETTING = 'antigravity_priority_native_offset'


def sql(run, database, query):
    raw = run(['docker', 'exec', '-i', database, 'sh', '-c',
               'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'], query.encode())
    lines = [line for line in raw.decode().splitlines() if line.strip().startswith('{')]
    if len(lines) != 1:
        raise RuntimeError('Priority migration returned an unexpected result')
    return json.loads(lines[0])


def read_offset(run, database):
    result = sql(run, database, "SELECT json_build_object('offset', COALESCE((SELECT value FROM settings WHERE key='" + SETTING + "'), '0'));")
    if result['offset'] not in ['0', '1000']:
        raise RuntimeError('Unknown native priority mapping version')
    return int(result['offset'])


def validate_marker(state, offset):
    file = pathlib.Path(state) / 'priority-mapping.json'
    if not file.exists() and offset == 0:
        return
    try:
        marker = json.loads(file.read_text())
    except (OSError, ValueError):
        raise RuntimeError('Local priority mapping marker is missing or invalid') from None
    if marker != {'schema_version': 1, 'native_offset': offset}:
        raise RuntimeError('Database and local priority mapping versions differ')


def migration_sql(target):
    if type(target) is not int or target not in [0, OFFSET]:
        raise RuntimeError('Unsupported target priority mapping')
    # A transaction-level lock makes a committed conversion safe to repeat even
    # if the process stops before its local marker is written. No auth columns,
    # concurrency limits or learning state are changed.
    return """BEGIN;
SET LOCAL lock_timeout='10s';
SELECT pg_advisory_xact_lock(718182910);
CREATE TEMP TABLE priority_mapping_result(previous_offset integer, target_offset integer, changed_accounts bigint) ON COMMIT DROP;
DO $priority_mapping$
DECLARE previous_value text; previous_offset integer; total bigint;
BEGIN
  SELECT value INTO previous_value FROM settings WHERE key='antigravity_priority_native_offset' FOR UPDATE;
  previous_value := COALESCE(previous_value, '0');
  IF previous_value NOT IN ('0', '1000') THEN
    RAISE EXCEPTION 'unknown priority mapping version';
  END IF;
  previous_offset := previous_value::integer;
  IF previous_offset <> TARGET_OFFSET THEN
    IF TARGET_OFFSET = 1000 AND EXISTS (
      SELECT 1 FROM account_groups managed_binding
      JOIN accounts managed ON managed.id=managed_binding.account_id
      JOIN account_groups other_binding ON other_binding.group_id=managed_binding.group_id
      JOIN accounts peer ON peer.id=other_binding.account_id
      WHERE managed.platform='antigravity' AND managed.type='oauth' AND managed.deleted_at IS NULL
      AND peer.deleted_at IS NULL AND NOT (peer.platform='antigravity' AND peer.type='oauth')
    ) THEN
      RAISE EXCEPTION 'mixed account group requires explicit priority review';
    END IF;
    SELECT count(*) INTO total FROM accounts WHERE platform='antigravity' AND type='oauth' AND deleted_at IS NULL;
    WITH updated AS (
      UPDATE accounts SET priority=priority+(TARGET_OFFSET-previous_offset), updated_at=NOW()
      WHERE platform='antigravity' AND type='oauth' AND deleted_at IS NULL RETURNING id
    )
    INSERT INTO scheduler_outbox(event_type,payload)
    SELECT 'account_bulk_changed', jsonb_build_object('account_ids',jsonb_agg(id)) FROM updated HAVING count(*)>0;
  ELSE
    total := 0;
  END IF;
  INSERT INTO settings(key,value,updated_at) VALUES('antigravity_priority_native_offset','TARGET_OFFSET',NOW())
  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at;
  INSERT INTO priority_mapping_result VALUES(previous_offset,TARGET_OFFSET,total);
END $priority_mapping$;
SELECT json_build_object('previous_offset',previous_offset,'target_offset',target_offset,'changed_accounts',changed_accounts) FROM priority_mapping_result;
COMMIT;
""".replace('TARGET_OFFSET', str(target))


def migrate(run, database, state, target=OFFSET):
    result = sql(run, database, migration_sql(target))
    state = pathlib.Path(state)
    marker = json.dumps({'schema_version': 1, 'native_offset': target}).encode()
    fd, temporary = tempfile.mkstemp(prefix='.priority-mapping-', dir=state)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        mode = state.stat()
        os.chown(temporary, mode.st_uid, mode.st_gid)
        os.replace(temporary, state / 'priority-mapping.json')
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    return result
