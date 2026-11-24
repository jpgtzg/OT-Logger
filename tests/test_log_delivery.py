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

    def test_opc_timestamps_preserved_separately_on_retry(self):
        sample = row()
        data = sample['payload']['data']
        data['source_timestamp'] = '2026-11-24T16:10:42.012345+00:00'
        data['server_timestamp'] = '2026-11-24T10:10:42.987654-06:00'
        bodies = [core.logs_body([sample], 'p', 'k') for _ in range(2)]
        self.assertEqual(bodies[0], bodies[1])
        record = bodies[0]['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        attrs = {a['key']: a['value']['stringValue'] for a in record['attributes'] if 'stringValue' in a['value']}
        self.assertEqual(attrs['source_timestamp'], data['source_timestamp'])
        self.assertEqual(attrs['server_timestamp'], data['server_timestamp'])
        self.assertEqual(attrs['logger_timestamp'], sample['sample_timestamp'])
        self.assertEqual(record['timeUnixNano'], core.nano(sample['sample_timestamp']))
        metric = json.dumps(core.request_body([sample], 'p', 'k'))
        self.assertNotIn('source_timestamp', metric)
        self.assertNotIn('server_timestamp', metric)

    def test_missing_opc_times_not_fabricated(self):
        sample = row()
        sample['payload']['data']['source_timestamp'] = None
        record = core.logs_body([sample], 'p', 'k')['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        attrs = {a['key']: a['value'] for a in record['attributes']}
        self.assertNotIn('source_timestamp', attrs)
        self.assertNotIn('server_timestamp', attrs)
        self.assertIn('logger_timestamp', attrs)

    def test_opc_time_requires_explicit_timezone(self):
        for invalid in ['2026-11-24T16:10:42', 'not-a-time']:
            sample = row()
            sample['payload']['data']['server_timestamp'] = invalid
            with self.assertRaises(ValueError):
                core.logs_body([sample], 'p', 'k')

    def test_all_dashboard_fields_and_original_quality(self):
        sample = row()
        sample['payload'].update(asset='K1', node='RX')
        sample['payload']['data'].update(
            source_timestamp='2026-11-24T16:10:41.123456Z',
            server_timestamp='2026-11-24T16:10:42.123456Z', status_code='BadNoCommunication')
        body = core.logs_body([sample], 'p', 'k')
        attrs = {a['key']: a['value'] for a in body['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']}
        for key in ['tag', 'value', 'source_timestamp', 'server_timestamp',
                    'logger_timestamp', 'status_code', 'asset', 'node']:
            self.assertIn(key, attrs)
        self.assertEqual(attrs['tag']['stringValue'], sample['payload']['data']['address'])
        self.assertEqual(attrs['value']['doubleValue'], 1.0)
        self.assertEqual(attrs['status_code']['stringValue'], 'BadNoCommunication')
        self.assertEqual(attrs['asset']['stringValue'], 'K1')
        self.assertEqual(attrs['node']['stringValue'], 'RX')
        self.assertNotIn('event.type', attrs)
        self.assertIn('otlogger.event_type', attrs)
        sample['payload'].pop('asset'); sample['payload'].pop('node')
        sample['payload']['data']['status_code'] = None
        record = core.logs_body([sample], 'p', 'k')['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        keys = {a['key'] for a in record['attributes']}
        self.assertTrue({'asset', 'node', 'status_code'}.isdisjoint(keys))

    def test_csv_dimensions_remain_explicit(self):
        text = 'address,tag,metric_name,unit,publish_as,asset,node\na,b,otlogger.h,1,metrics|logs,K1,RX\n'
        item = selector.parse_selector(text)[0]
        self.assertEqual((item['asset'], item['node']), ('K1', 'RX'))
        old = selector.parse_selector('address,tag,metric_name,unit\na,b,otlogger.h,1')[0]
        self.assertEqual((old['asset'], old['node']), ('', ''))

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
