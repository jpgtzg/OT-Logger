-- Stop publisher before applying. Requires 004. No changes to existing deliveries.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE telemetry_export_tags ADD COLUMN asset TEXT NOT NULL DEFAULT '';
ALTER TABLE telemetry_export_tags ADD COLUMN node TEXT NOT NULL DEFAULT '';
CREATE OR REPLACE FUNCTION enqueue_otlogger_tag() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selection telemetry_export_tags%ROWTYPE;
BEGIN
 SELECT * INTO selection FROM telemetry_export_tags WHERE address=NEW.address AND tag=NEW.tag;
 IF NOT FOUND THEN RETURN NEW; END IF;
 IF NEW.sample_id IS NULL THEN RAISE EXCEPTION 'sample_id must not be null'; END IF;
 INSERT INTO telemetry_outbox(sample_id,signal,source_table,sample_timestamp,payload)
 SELECT NEW.sample_id,delivery_signal,'tags',NEW.logger_timestamp,
   jsonb_build_object('schema_version',1,'data',to_jsonb(NEW)-'sample_id',
    'metric_name',selection.metric_name,'unit',selection.unit,'asset',selection.asset,'node',selection.node)
 FROM (SELECT DISTINCT unnest(selection.publish_as) AS delivery_signal) signals;
 RETURN NEW;
END $$;
COMMIT;
