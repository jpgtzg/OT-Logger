-- Repair an already installed 001 migration; do not rerun 001.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE OR REPLACE FUNCTION enqueue_otlogger_sample() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.sample_id IS NULL THEN RAISE EXCEPTION 'sample_id must not be null'; END IF;
 INSERT INTO telemetry_outbox(sample_id,source_table,sample_timestamp,payload)
 VALUES (NEW.sample_id,TG_ARGV[0],NEW.timestamp,
   jsonb_build_object('schema_version',1,'data',to_jsonb(NEW)-'sample_id'));
 RETURN NEW;
END $$;
DROP TRIGGER cpu_outbox ON cpu_usage;
DROP TRIGGER ram_outbox ON ram_usage;
CREATE TRIGGER cpu_outbox AFTER INSERT ON cpu_usage FOR EACH ROW
 EXECUTE FUNCTION enqueue_otlogger_sample('cpu_usage');
CREATE TRIGGER ram_outbox AFTER INSERT ON ram_usage FOR EACH ROW
 EXECUTE FUNCTION enqueue_otlogger_sample('ram_usage');
COMMIT;
