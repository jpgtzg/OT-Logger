import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1] / 'publisher'
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
core, selector = load('core'), load('selector')

def row(signal='logs'):
    return {'sample_id': '34a1c07e-161e-4b83-8160-6e176356b2b7', 'signal': signal,
            'source_table': 'tags', 'sample_timestamp': '2026-11-24T16:10:43.123456Z',
            'payload': {'schema_version': 1, 'metric_name': 'otlogger.heartbeat', 'unit': '1',
                        'data': {'value': 1.0, 'address': 'ns=2;s=test', 'tag': 'heartbeat'}}}

class LogDeliveryTests(unittest.TestCase):
    def test_selector_compatibility_and_signals(self):
        header = 'address,tag,metric_name,unit'
        self.assertEqual(selector.parse_selector(header+'\na,b,otlogger.h,1\n')[0]['publish_as'], ['metrics'])
        for value in ['logs', 'metrics', 'metrics|logs']:
            self.assertEqual(selector.parse_selector(header+',publish_as\na,b,otlogger.h,1,'+value)[0]['publish_as'], value.split('|'))
        for value in ['', 'traces', 'logs|logs']:
            with self.assertRaises(ValueError):
                selector.parse_selector(header+',publish_as\na,b,otlogger.h,1,'+value)

    def test_original_identity_timestamp_and_numeric_value(self):
        record = core.logs_body([row()], 'publisher', 'kep')['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        attrs = {a['key']: a['value'] for a in record['attributes']}
        self.assertEqual(attrs['measurement.id']['stringValue'], row()['sample_id'])
        self.assertEqual(attrs['tag.value']['doubleValue'], 1.0)
        self.assertEqual(record['timeUnixNano'], core.nano(row()['sample_timestamp']))
        self.assertNotIn('measurement.id', json.dumps(core.request_body([row()], 'publisher', 'kep')))
        for value in ['NaN', 'Infinity', 'text']:
            bad = row(); bad['payload']['data']['value'] = value
            with self.assertRaises(ValueError): core.logs_body([bad], 'p', 'k')

    def test_partial_logs_ack(self):
        self.assertTrue(core.full_ack(200, {'partialSuccess': {}}, 'logs'))
        for value in ['2', 'invalid', None]:
            self.assertFalse(core.full_ack(200, {'partialSuccess': {'rejectedLogRecords': value}}, 'logs'))

    def test_metric_success_is_not_reset_when_logs_fail(self):
        psycopg = types.ModuleType('psycopg'); psycopg.Error = type('Error', (Exception,), {})
        rows_module = types.ModuleType('psycopg.rows'); rows_module.dict_row = object()
        outcomes = []
        class Context:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Result:
            def __init__(self, values=()): self.values = values
            def fetchall(self): return self.values
        class Conn(Context):
            def transaction(self): return Context()
            def execute(self, sql, params=None):
                if 'RETURNING o.*' in sql: return Result([row('metrics'), row('logs')])
                if 'collector_accepted_at=CASE' in sql:
                    outcomes.append((params[0], params[-1]))
                    self.assert_signal(sql)
                return Result()
            def assert_signal(self, sql):
                assert "attempt_id=%s AND signal=%s AND status='inflight'" in sql
        psycopg.connect = lambda **kwargs: Conn()
        with patch.dict(sys.modules, {'psycopg': psycopg, 'psycopg.rows': rows_module,
                                     'core': core, 'selector': selector}):
            main = load('main')
        class Response(Context):
            status = 200
            def read(self, *args): return b'{}'
        def urlopen(request, **kwargs):
            if request.full_url.endswith('/v1/logs'):
                raise urllib.error.HTTPError(request.full_url, 503, 'unavailable', {}, None)
            return Response()
        def stop_after_cycle(*args): main.stop.set()
        with patch.dict('os.environ', {'OTEL_EXPORTER_OTLP_METRICS_ENDPOINT': 'http://collector:4318/v1/metrics',
                                      'OTLOGGER_SOURCE': 'kep'}, clear=True), \
             patch.object(main.signal, 'signal'), \
             patch.object(main.urllib.request, 'urlopen', side_effect=urlopen), \
             patch.object(main.stop, 'wait', side_effect=stop_after_cycle):
            main.main()
        self.assertEqual(outcomes, [('collector_accepted', 'metrics'), ('pending', 'logs')])
