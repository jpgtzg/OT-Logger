-- Requires 001 and corrected CPU/RAM triggers. One-time DEV migration.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE telemetry_export_tags (
 address TEXT NOT NULL,
 tag TEXT NOT NULL,
 metric_name TEXT NOT NULL UNIQUE,
 unit TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(address,tag)
);
ALTER TABLE telemetry_outbox DROP CONSTRAINT telemetry_outbox_source_table_check;
ALTER TABLE telemetry_outbox ADD CONSTRAINT telemetry_outbox_source_table_check
 CHECK(source_table IN ('cpu_usage','ram_usage','tags'));
ALTER TABLE tags ADD COLUMN sample_id UUID;
ALTER TABLE tags ALTER COLUMN sample_id SET DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX tags_sample_identity ON tags(logger_timestamp,sample_id);
CREATE FUNCTION enqueue_otlogger_tag() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selection telemetry_export_tags%ROWTYPE;
BEGIN
 SELECT * INTO selection FROM telemetry_export_tags WHERE address=NEW.address AND tag=NEW.tag;
 IF NOT FOUND THEN RETURN NEW; END IF;
 IF NEW.sample_id IS NULL THEN RAISE EXCEPTION 'sample_id must not be null'; END IF;
 INSERT INTO telemetry_outbox(sample_id,source_table,sample_timestamp,payload)
 VALUES(NEW.sample_id,'tags',NEW.logger_timestamp,
   jsonb_build_object('schema_version',1,'data',to_jsonb(NEW)-'sample_id',
    'metric_name',selection.metric_name,'unit',selection.unit));
 RETURN NEW;
END $$;
CREATE TRIGGER tags_outbox AFTER INSERT ON tags FOR EACH ROW EXECUTE FUNCTION enqueue_otlogger_tag();
GRANT SELECT,INSERT,DELETE ON telemetry_export_tags TO otlogger_publisher;
COMMIT;
