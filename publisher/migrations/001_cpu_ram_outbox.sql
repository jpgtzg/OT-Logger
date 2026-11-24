-- Apply to an initialized OTLogger database in DEV. No historical backfill.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE telemetry_outbox (
 sample_id UUID PRIMARY KEY,
 source_table TEXT NOT NULL CHECK (source_table IN ('cpu_usage','ram_usage')),
 sample_timestamp TIMESTAMPTZ NOT NULL,
 payload JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','inflight','collector_accepted','expired','rejected')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ,
 attempt_id UUID,
 collector_accepted_at TIMESTAMPTZ,
 last_error TEXT
);
CREATE INDEX outbox_pending ON telemetry_outbox(next_attempt_at,created_at) WHERE status='pending';
CREATE INDEX outbox_inflight ON telemetry_outbox(lease_until) WHERE status='inflight';
CREATE INDEX outbox_accepted ON telemetry_outbox(collector_accepted_at) WHERE status='collector_accepted';
-- Existing rows remain NULL. Generate identities only for new inserts, without
-- rewriting the historical hypertables during this migration.
ALTER TABLE cpu_usage ADD COLUMN sample_id UUID;
ALTER TABLE cpu_usage ALTER COLUMN sample_id SET DEFAULT gen_random_uuid();
ALTER TABLE ram_usage ADD COLUMN sample_id UUID;
ALTER TABLE ram_usage ALTER COLUMN sample_id SET DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX cpu_sample_identity ON cpu_usage(timestamp,sample_id);
CREATE UNIQUE INDEX ram_sample_identity ON ram_usage(timestamp,sample_id);
CREATE FUNCTION enqueue_otlogger_sample() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.sample_id IS NULL THEN RAISE EXCEPTION 'sample_id must not be null'; END IF;
 INSERT INTO telemetry_outbox(sample_id,source_table,sample_timestamp,payload)
 VALUES (NEW.sample_id,TG_ARGV[0],NEW.timestamp,
   jsonb_build_object('schema_version',1,'data',to_jsonb(NEW)-'sample_id'));
 RETURN NEW;
END $$;
CREATE TRIGGER cpu_outbox AFTER INSERT ON cpu_usage FOR EACH ROW EXECUTE FUNCTION enqueue_otlogger_sample('cpu_usage');
CREATE TRIGGER ram_outbox AFTER INSERT ON ram_usage FOR EACH ROW EXECUTE FUNCTION enqueue_otlogger_sample('ram_usage');
COMMIT;
