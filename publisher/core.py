"""Pure OTLP conversion. Sample IDs deliberately stay out of metric dimensions."""
import math
import uuid
from datetime import datetime, timezone


def nano(stamp):
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)


def request_body(rows, service, source, pod_uid=None):
    metrics = {}
    definitions = {
        'cpu_usage': [('usage', 'otlogger.cpu.usage', '%', 'asDouble')],
        'ram_usage': [('total_kb', 'otlogger.ram.total', 'KiBy', 'asInt'),
                      ('free_kb', 'otlogger.ram.free', 'KiBy', 'asInt')],
    }
    for row in rows:
        if row['payload']['schema_version'] != 1:
            raise ValueError('Unsupported payload schema')
        data = row['payload']['data']
        fields = definitions[row['source_table']] if row['source_table'] != 'tags' else [
            ('value', row['payload']['metric_name'], row['payload']['unit'], 'asDouble')]
        for column, name, unit, field in fields:
            value = float(data[column]) if field == 'asDouble' else str(int(data[column]))
            if field == 'asDouble' and not math.isfinite(value):
                raise ValueError('Nonfinite numeric sample')
            point = {'timeUnixNano': nano(row['sample_timestamp']), field: value}
            metrics.setdefault(name, {'name': name, 'unit': unit, 'gauge': {'dataPoints': []}})['gauge']['dataPoints'].append(point)
    attrs = [('service.name', service), ('otlogger.source', source)]
    if pod_uid:
        attrs.append(('k8s.pod.uid', pod_uid))
    return {'resourceMetrics': [{'resource': {'attributes': [
        {'key': k, 'value': {'stringValue': v}} for k, v in attrs]},
        'scopeMetrics': [{'scope': {'name': 'otlogger.publisher', 'version': '0.3.0'},
                          'metrics': list(metrics.values())}]}]}


def logs_body(rows, service, source, pod_uid=None):
    records = []
    for row in rows:
        if row['source_table'] != 'tags' or row['payload']['schema_version'] != 1:
            raise ValueError('Logs require a numeric tag sample')
        data = row['payload']['data']
        value = float(data['value'])
        if not math.isfinite(value):
            raise ValueError('Nonfinite numeric sample')
        identity = str(uuid.UUID(str(row['sample_id'])))
        attrs = {'event.type': 'industrial.tag.sample', 'measurement.id': identity,
                 'tag.name': row['payload']['metric_name'], 'tag.address': data['address'],
                 'tag.source_name': data['tag'], 'tag.unit': row['payload']['unit']}
        records.append({'timeUnixNano': nano(row['sample_timestamp']),
                        'severityText': 'INFO', 'severityNumber': 9,
                        'body': {'stringValue': 'OTLogger numeric tag sample'},
                        'attributes': [{'key': k, 'value': {'stringValue': v}} for k, v in attrs.items()] +
                                      [{'key': 'tag.value', 'value': {'doubleValue': value}}]})
    attrs = [('service.name', service), ('otlogger.source', source)]
    if pod_uid:
        attrs.append(('k8s.pod.uid', pod_uid))
    return {'resourceLogs': [{'resource': {'attributes': [
        {'key': k, 'value': {'stringValue': v}} for k, v in attrs]},
        'scopeLogs': [{'scope': {'name': 'otlogger.publisher', 'version': '0.3.0'},
                       'logRecords': records}]}]}


def full_ack(status, ack, signal='metrics'):
    if status != 200 or not isinstance(ack, dict):
        return False
    partial = ack.get('partialSuccess', {})
    if not isinstance(partial, dict):
        return False
    try:
        return int(partial.get('rejectedLogRecords' if signal == 'logs' else 'rejectedDataPoints', 0)) == 0
    except (ValueError, TypeError):
        return False
