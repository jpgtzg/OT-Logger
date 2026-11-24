import importlib.util
from pathlib import Path
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'publisher' / f'{name}.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


selector, core = load('selector'), load('core')
HEADER = 'address,tag,metric_name,unit\n'


class TagSelectorTests(unittest.TestCase):
    def test_exact_identity_and_empty_selection(self):
        self.assertEqual(selector.parse_selector(HEADER), [])
        rows = selector.parse_selector(HEADER+'ns=2;s=A,Heartbeat,otlogger.a,1\nns=2;s=B,Heartbeat,otlogger.b,1\n')
        self.assertEqual(len(rows), 2)

    def test_duplicate_and_reserved_metric_rejected(self):
        for body in ('a,x,otlogger.a,1\na,x,otlogger.b,1\n',
                     'a,x,otlogger.cpu.usage,1\n', 'a,x,otlogger.a,1\nb,y,otlogger.a,1\n'):
            with self.assertRaises(ValueError):
                selector.parse_selector(HEADER+body)

    def test_float_tag_with_original_logger_timestamp(self):
        row = {'source_table': 'tags', 'sample_timestamp': '2026-11-20T10:00:00Z',
               'payload': {'schema_version': 1, 'metric_name': 'otlogger.heartbeat',
                           'unit': '1', 'data': {'value': '42.25'}}}
        metric = core.request_body([row], 'publisher', 'kep')['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]
        self.assertEqual(metric['gauge']['dataPoints'][0]['asDouble'], 42.25)
        self.assertEqual(metric['name'], 'otlogger.heartbeat')
        for value in ('bad', 'NaN', 'Infinity'):
            row['payload']['data']['value'] = value
            with self.assertRaises(ValueError):
                core.request_body([row], 'publisher', 'kep')
