import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('publisher_core', Path(__file__).resolve().parents[1] / 'publisher/core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class PublisherTests(unittest.TestCase):
    def test_preserves_repeated_samples_and_original_time(self):
        row = {'sample_timestamp': '2026-11-20T10:00:00.123456+00:00', 'source_table': 'cpu_usage',
               'payload': {'schema_version': 1, 'data': {'usage': 14.2}}}
        body = core.request_body([row, row], 'publisher', 'kepserver_pcn', 'pod-uid')
        resource = body['resourceMetrics'][0]
        points = resource['scopeMetrics'][0]['metrics'][0]['gauge']['dataPoints']
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]['asDouble'], 14.2)
        self.assertEqual(points[0]['timeUnixNano'], core.nano('2026-11-20T12:00:00.123456+02:00'))
        self.assertNotIn('sample_id', json.dumps(body))
        self.assertIn('k8s.pod.uid', json.dumps(resource['resource']))

    def test_ram_units_and_integer_precision(self):
        row = {'sample_timestamp': '2026-11-20T10:00:00Z', 'source_table': 'ram_usage',
               'payload': {'schema_version': 1, 'data': {'total_kb': 2**54, 'free_kb': 512}}}
        metrics = core.request_body([row], 'publisher', 'kep')['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        self.assertEqual(metrics[0]['unit'], 'KiBy')
        self.assertEqual(metrics[0]['gauge']['dataPoints'][0]['asInt'], str(2**54))

    def test_full_and_partial_confirmations(self):
        for ack in ({}, {'partialSuccess': {}}, {'partialSuccess': {'rejectedDataPoints': '0'}}):
            self.assertTrue(core.full_ack(200, ack))
        for ack in ({'partialSuccess': {'rejectedDataPoints': '1'}}, [], {'partialSuccess': None}):
            self.assertFalse(core.full_ack(200, ack))
        self.assertFalse(core.full_ack(503, {}))

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            core.nano('2026-11-20T10:00:00')


if __name__ == '__main__':
    unittest.main()
