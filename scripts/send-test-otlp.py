#!/usr/bin/env python3
"""Send the raw CPU/RAM test CSVs as OTLP gauges to the local test receiver."""
import argparse
import csv
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path


def timestamp_ns(value):
    stamp = datetime.fromisoformat(re.sub(r'([+-]\d{2})$', r'\1:00', value.replace('Z', '+00:00')))
    if stamp.tzinfo is None:
        raise ValueError(f'Missing timezone: {value}')
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str(((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=Path)
    parser.add_argument('--port', type=int, default=4318)
    parser.add_argument('--endpoint', default=os.getenv('OTEL_EXPORTER_OTLP_METRICS_ENDPOINT'),
                        help='Full OTLP/HTTP metrics URL, including /v1/metrics')
    parser.add_argument('--max-age-seconds', type=int, help='Reject stale batches without changing timestamps')
    args = parser.parse_args()
    endpoint = args.endpoint or f'http://127.0.0.1:{args.port}/v1/metrics'
    url = urlsplit(endpoint)
    if url.scheme not in ('http', 'https') or not url.hostname or url.path != '/v1/metrics':
        parser.error('Expected an HTTP(S) endpoint ending in /v1/metrics')
    if args.max_age_seconds is not None and args.max_age_seconds <= 0:
        parser.error('--max-age-seconds must be positive')
    now_ns = int(timestamp_ns(datetime.now(timezone.utc).isoformat()))
    metrics = []
    for table, column, name, unit, field, parse in (
        ('cpu_usage', 'usage', 'otlogger.cpu.usage', '%', 'asDouble', float),
        ('ram_usage', 'total_kb', 'otlogger.ram.total', 'KiBy', 'asInt', lambda x: str(int(x))),
        ('ram_usage', 'free_kb', 'otlogger.ram.free', 'KiBy', 'asInt', lambda x: str(int(x))),
    ):
        with (args.batch / f'{table}.csv').open(newline='') as file:
            points = [{'timeUnixNano': timestamp_ns(row['timestamp']), field: parse(row[column])}
                      for row in csv.DictReader(file)]
        if not points:
            raise ValueError(f'Empty batch: {table}')
        if args.max_age_seconds is not None and any(
            now_ns - int(point['timeUnixNano']) >= args.max_age_seconds * 10**9 for point in points
        ):
            raise ValueError(f'{table}: lote fuera de la ventana de envío; los CSV se conservan')
        metrics.append({'name': name, 'unit': unit, 'gauge': {'dataPoints': points}})
    attributes = [{'key': k, 'value': {'stringValue': v}} for k, v in (
        ('service.name', os.getenv('OTEL_SERVICE_NAME', 'otlogger-publisher-test')), ('otlogger.site', 'Balcones'),
        ('otlogger.source', 'kepserver_pcn'))]
    if pod_uid := os.getenv('POD_UID'):
        attributes.append({'key': 'k8s.pod.uid', 'value': {'stringValue': pod_uid}})
    body = {'resourceMetrics': [{'resource': {'attributes': attributes},
                                 'scopeMetrics': [{'scope': {'name': 'otlogger.pilot'}, 'metrics': metrics}]}]}
    request = urllib.request.Request(endpoint,
                                     data=json.dumps(body, allow_nan=False).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        ack = json.load(response)
        if response.status != 200 or not isinstance(ack, dict):
            raise ValueError('Confirmación OTLP inválida')
        partial = ack.get('partialSuccess', {})
        if not isinstance(partial, dict) or int(partial.get('rejectedDataPoints', 0)) != 0:
            raise ValueError('El receptor rechazó puntos; el lote no se confirma')
        if partial.get('errorMessage'):
            print(f"Aviso del receptor: {partial['errorMessage']}")
    print('Lote confirmado por el receptor OTLP')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Envío OTLP fallido: {exc}') from None
