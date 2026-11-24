"""CSV allowlist for numeric OPC UA tags. Exact address + tag identity."""
import csv
import io
import re


def parse_selector(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    fields = reader.fieldnames or []
    if (fields[:4] != ['address', 'tag', 'metric_name', 'unit']
            or len(fields) != len(set(fields))
            or set(fields[4:]) - {'publish_as', 'asset', 'node'}):
        raise ValueError('Expected address,tag,metric_name,unit and optional publish_as,asset,node')
    rows, identities, metrics = [], set(), set()
    reserved = {'otlogger.cpu.usage', 'otlogger.ram.total', 'otlogger.ram.free'}
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ValueError('Invalid CSV row')
        row = {k: v.strip() for k, v in row.items()}
        signals = row.get('publish_as', 'metrics').split('|')
        if not signals or len(set(signals)) != len(signals) or set(signals) - {'metrics', 'logs'}:
            raise ValueError('publish_as must be metrics, logs or metrics|logs')
        row['publish_as'] = signals
        row.setdefault('asset', '')
        row.setdefault('node', '')
        identity = (row['address'], row['tag'])
        name = row['metric_name']
        if not all(identity) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.]{0,199}', name):
            raise ValueError('Invalid address, tag or metric name')
        if identity in identities or name in metrics or name in reserved:
            raise ValueError('Duplicate identity or metric name')
        identities.add(identity)
        metrics.add(name)
        rows.append(row)
    return rows
