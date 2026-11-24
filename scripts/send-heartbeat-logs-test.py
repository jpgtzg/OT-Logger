#!/usr/bin/env python3
"""Synthetic OTLP Logs PoC; does not read KepServer or TimescaleDB."""
import argparse
import json
import math
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit
import uuid


def full_ack(status, ack):
    if status != 200 or not isinstance(ack, dict):
        return False
    partial = ack.get('partialSuccess', {})
    if not isinstance(partial, dict):
        return False
    try:
        return int(partial.get('rejectedLogRecords', 0)) == 0
    except (TypeError, ValueError):
        return False


def sample(run_id, phase, value, sequence):
    attrs = {
        'event.type': 'industrial.heartbeat.test',
        'measurement.id': str(uuid.uuid4()),
        'test.run_id': run_id,
        'test.phase': phase,
        'test.sequence': sequence,
        'test.synthetic': True,
        'tag.name': 'otlogger.test.heartbeat',
        'tag.value': float(value),
    }
    def typed(value):
        if isinstance(value, bool):
            return {'boolValue': value}
        if isinstance(value, int):
            return {'intValue': str(value)}
        if isinstance(value, float):
            return {'doubleValue': value}
        return {'stringValue': value}
    return {
        'timeUnixNano': str(time.time_ns()),
        'severityNumber': 9,
        'severityText': 'INFO',
        'body': {'stringValue': 'OTLogger synthetic heartbeat sample'},
        'attributes': [{'key': k, 'value': typed(v)} for k, v in attrs.items()],
    }


def send(endpoint, records):
    attrs = [{'key': 'service.name', 'value': {'stringValue': 'otlogger-heartbeat-logs-test'}}]
    if os.getenv('POD_UID'):
        attrs.append({'key': 'k8s.pod.uid', 'value': {'stringValue': os.environ['POD_UID']}})
    body = {'resourceLogs': [{'resource': {'attributes': attrs}, 'scopeLogs': [{
        'scope': {'name': 'otlogger.heartbeat.poc'}, 'logRecords': records}]}]}
    request = urllib.request.Request(endpoint, data=json.dumps(body, allow_nan=False).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        ack = json.loads(raw) if raw else {}
        if not full_ack(response.status, ack):
            raise ValueError('OTLP logs rejected or partially accepted; inspect Collector')
        if ack.get('partialSuccess', {}).get('errorMessage'):
            print('Collector warning:', ack['partialSuccess']['errorMessage'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default=os.getenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT',
        'http://otel-collector.observability.svc.cluster.local:4318/v1/logs'))
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/heartbeat-evidence.jsonl'))
    args = parser.parse_args()
    url = urlsplit(args.endpoint)
    if url.scheme not in ('http', 'https') or not url.hostname or url.path != '/v1/logs':
        parser.error('Expected HTTP(S) /v1/logs endpoint')
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error('--interval must be finite and positive')
    run_id = str(uuid.uuid4())
    print(f'run_id={run_id} interval={args.interval}s evidence={args.evidence}', flush=True)
    sequence = 0
    with args.evidence.open('a') as evidence:
        def capture(phase, value):
            nonlocal sequence
            sequence += 1
            record = sample(run_id, phase, value, sequence)
            evidence.write(json.dumps(record) + '\n')
            evidence.flush()
            os.fsync(evidence.fileno())
            return record

        def publish(records):
            send(args.endpoint, records)
            print(f'collector_accepted records={len(records)} '
                  f'original_times={[r["timeUnixNano"] for r in records]}', flush=True)

        for phase, values in [('alternating', [0, 1, 0, 1]), ('stuck', [1, 1, 1])]:
            print(f'phase={phase}', flush=True)
            for value in values:
                publish([capture(phase, value)])
                time.sleep(args.interval)
        print('phase=missing: no samples generated for two intervals', flush=True)
        time.sleep(2 * args.interval)
        print('phase=buffered: hold two samples locally before sending', flush=True)
        pending = []
        for value in [0, 1]:
            pending.append(capture('buffered', value))
            time.sleep(args.interval)
        publish(pending)
        for value in [0, 1]:
            publish([capture('resumed', value)])
            time.sleep(args.interval)
    print(f'finished run_id={run_id} generated={sequence}; '
          'Collector ACK is not proof of Dynatrace persistence. '
          'This PoC does not implement durable retries.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Heartbeat Logs PoC failed: {exc}') from None
