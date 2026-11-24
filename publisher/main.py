"""Continuous transactional outbox publisher. Requires explicit DEV migration."""
import json
import logging
import os
from pathlib import Path
import signal
import threading
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from selector import parse_selector
from core import full_ack, request_body, logs_body

log = logging.getLogger('otlogger.publisher')
stop = threading.Event()


def main():
    logging.basicConfig(level=logging.INFO)
    endpoint = os.environ['OTEL_EXPORTER_OTLP_METRICS_ENDPOINT']
    url = urlsplit(endpoint)
    if url.scheme not in ('http', 'https') or url.path != '/v1/metrics' or not url.hostname:
        raise ValueError('Expected full HTTP(S) /v1/metrics endpoint')
    logs_endpoint = os.getenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', url._replace(path='/v1/logs', query='', fragment='').geturl())
    logs_url = urlsplit(logs_endpoint)
    if logs_url.scheme not in ('http', 'https') or logs_url.path != '/v1/logs' or not logs_url.hostname:
        raise ValueError('Expected full HTTP(S) /v1/logs endpoint')
    source = os.environ['OTLOGGER_SOURCE']  # measured server, NOT publisher pod
    service = os.getenv('OTEL_SERVICE_NAME', 'otlogger-publisher')
    size = int(os.getenv('PUBLISH_BATCH_SIZE', '100'))
    poll = float(os.getenv('PUBLISH_POLL_SECONDS', '5'))
    max_age = int(os.getenv('PUBLISH_MAX_AGE_SECONDS', '3600'))
    if not 1 <= size <= 1000 or poll <= 0 or max_age <= 0:
        raise ValueError('Invalid batch, poll or age limits')
    selector_path = os.getenv('PUBLISH_TAGS_CSV')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            # libpq uses PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD, avoiding
            # interpolation of passwords in connection strings or logs.
            with psycopg.connect(connect_timeout=10, autocommit=True, row_factory=dict_row) as conn:
                conn.execute("SET statement_timeout = '15s'")
                selector_content = None
                while not stop.is_set():
                    if selector_path:
                        try:
                            content = Path(selector_path).read_text()
                            if content != selector_content:
                                selection = parse_selector(content)
                                with conn.transaction():
                                    conn.execute('DELETE FROM telemetry_export_tags')
                                    for item in selection:
                                        conn.execute('INSERT INTO telemetry_export_tags (address,tag,metric_name,unit,publish_as) VALUES (%s,%s,%s,%s,%s)',
                                            (item['address'],item['tag'],item['metric_name'],item['unit'],item['publish_as']))
                                selector_content = content
                                log.info('tag_selector_entries=%d', len(selection))
                        except (OSError, ValueError):
                            log.error('Invalid/unavailable tags CSV; keeping previous selection')
                    # Claim durable work atomically. An expired lease is reclaimed
                    # after a crash; old attempts cannot acknowledge a new claim.
                    token = uuid.uuid4()
                    with conn.transaction():
                        conn.execute("""UPDATE telemetry_outbox SET status='pending',attempt_id=NULL,lease_until=NULL
                            WHERE status='inflight' AND lease_until < now()""")
                        conn.execute("""UPDATE telemetry_outbox SET status='expired',last_error='Outside backend timestamp window'
                            WHERE status='pending' AND sample_timestamp <= now() - %s * interval '1 second'""", (max_age,))
                        rows = conn.execute("""WITH chosen AS (
                            SELECT sample_id,signal FROM telemetry_outbox WHERE status='pending' AND next_attempt_at<=now()
                            ORDER BY created_at,sample_id FOR UPDATE SKIP LOCKED LIMIT %s)
                            UPDATE telemetry_outbox o SET status='inflight',attempt_id=%s,
                                lease_until=now()+interval '120 seconds',attempts=attempts+1
                            FROM chosen WHERE o.sample_id=chosen.sample_id AND o.signal=chosen.signal RETURNING o.*""", (size, token)).fetchall()
                    # Cleanup is bounded; expired/rejected/pending records never
                    # enter the accepted retention cleanup.
                    conn.execute("""DELETE FROM telemetry_outbox WHERE (sample_id,signal) IN (
                        SELECT sample_id,signal FROM telemetry_outbox WHERE status='collector_accepted'
                        AND collector_accepted_at<now()-interval '48 hours' LIMIT 1000)""")
                    counts = conn.execute('SELECT signal,status,count(*) AS count FROM telemetry_outbox GROUP BY signal,status').fetchall()
                    log.info('outbox_counts=%s', counts)
                    if not rows:
                        stop.wait(poll)
                        continue
                    for delivery_signal in ('metrics', 'logs'):
                        signal_rows = [r for r in rows if r['signal'] == delivery_signal]
                        if not signal_rows:
                            continue
                        builder = logs_body if delivery_signal == 'logs' else request_body
                        target = logs_endpoint if delivery_signal == 'logs' else endpoint
                        valid = []
                        for row in signal_rows:
                            try:
                                json.dumps(builder([row], service, source), allow_nan=False)
                            except (ValueError, KeyError, TypeError, OverflowError):
                                conn.execute("""UPDATE telemetry_outbox SET status='rejected',
                                    last_error='Invalid numeric sample or mapping',attempt_id=NULL,lease_until=NULL
                                    WHERE sample_id=%s AND signal=%s AND attempt_id=%s""", (row['sample_id'],delivery_signal,token))
                            else:
                                valid.append(row)
                        signal_rows = valid
                        if not signal_rows:
                            stop.wait(poll)
                            continue
                        outcome, error = 'pending', None
                        try:
                            body = json.dumps(builder(signal_rows, service, source, os.getenv('POD_UID')), allow_nan=False).encode()
                            req = urllib.request.Request(target, data=body, headers={'Content-Type': 'application/json'})
                            with urllib.request.urlopen(req, timeout=15) as response:
                                ack = json.loads(response.read(65536) or b'{}')
                                if not full_ack(response.status, ack, delivery_signal):
                                    raise ValueError('Partial/invalid OTLP acknowledgment; retry may duplicate accepted points')
                            outcome = 'collector_accepted'
                        except urllib.error.HTTPError as exc:
                            outcome = 'pending' if exc.code in (408,413,429,500,502,503,504) else 'rejected'
                            error = f'HTTP {exc.code}'
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            # Do not log payloads, credentials, or HTTP error bodies.
                            error = type(exc).__name__
                        with conn.transaction():
                            conn.execute("""UPDATE telemetry_outbox SET status=%s,last_error=%s,
                                collector_accepted_at=CASE WHEN %s='collector_accepted' THEN now() ELSE NULL END,
                                next_attempt_at=now()+least(300,power(2,least(attempts,8))) * interval '1 second',
                                attempt_id=NULL,lease_until=NULL
                                WHERE attempt_id=%s AND signal=%s AND status='inflight'""", (outcome,error,outcome,token,delivery_signal))
                        log.info('signal=%s batch_rows=%d outcome=%s error=%s', delivery_signal,len(signal_rows),outcome,error)
                    stop.wait(poll)
        except psycopg.Error as exc:
            log.error('Database unavailable: %s',type(exc).__name__)
            stop.wait(10)


if __name__ == '__main__':
    main()
