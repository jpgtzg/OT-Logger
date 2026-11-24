-- Stop the old publisher first. Requires 001/002 and 003. No historical backfill.
-- Existing CPU/RAM and tag deliveries remain metrics with their original state.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE telemetry_outbox ADD COLUMN signal TEXT NOT NULL DEFAULT 'metrics'
 CHECK (signal IN ('metrics','logs'));
ALTER TABLE telemetry_outbox DROP CONSTRAINT telemetry_outbox_pkey;
ALTER TABLE telemetry_outbox ADD PRIMARY KEY (sample_id,signal);
ALTER TABLE telemetry_export_tags ADD COLUMN publish_as TEXT[] NOT NULL DEFAULT ARRAY['metrics']::TEXT[]
 CHECK (cardinality(publish_as)>0 AND publish_as <@ ARRAY['metrics','logs']::TEXT[]
        AND array_position(publish_as,NULL) IS NULL);
CREATE OR REPLACE FUNCTION enqueue_otlogger_tag() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selection telemetry_export_tags%ROWTYPE;
BEGIN
 SELECT * INTO selection FROM telemetry_export_tags WHERE address=NEW.address AND tag=NEW.tag;
 IF NOT FOUND THEN RETURN NEW; END IF;
 IF NEW.sample_id IS NULL THEN RAISE EXCEPTION 'sample_id must not be null'; END IF;
 INSERT INTO telemetry_outbox(sample_id,signal,source_table,sample_timestamp,payload)
 SELECT NEW.sample_id,delivery_signal,'tags',NEW.logger_timestamp,
   jsonb_build_object('schema_version',1,'data',to_jsonb(NEW)-'sample_id',
    'metric_name',selection.metric_name,'unit',selection.unit)
 FROM (SELECT DISTINCT unnest(selection.publish_as) AS delivery_signal) signals;
 RETURN NEW;
END $$;
COMMIT;
