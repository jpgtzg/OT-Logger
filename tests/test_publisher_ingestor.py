"""Exercise the actual insert_many method without needing OPC UA or a DB server."""
import ast
from contextlib import nullcontext
from pathlib import Path
import unittest
import uuid

source = Path(__file__).resolve().parents[1] / 'ingestor/src/db.py'
tree = ast.parse(source.read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IngestorDatabase')
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'insert_many')
namespace = {'uuid': uuid}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)


class Writer:
    insert_many = namespace['insert_many']

    def __init__(self, enabled):
        self._sample_id_tables = enabled
        self.calls = []

    def _validate_identifier(self, name):
        assert name.replace('_', '').isalnum()

    def transaction(self):
        return nullcontext()

    def execute_many(self, query, rows):
        self.calls.append((query, rows))


class IngestorPublisherTests(unittest.TestCase):
    def test_retry_keeps_identity_and_preserves_tag_address(self):
        db = Writer({'cpu_usage', 'ram_usage'})
        batch = {'cpu_usage': [{'timestamp': 'now', 'usage': 10}],
                 'tags': [{'logger_timestamp': 'now', 'address': 'ns=2;s=test', 'tag': 'test', 'value': '1'}]}
        db.insert_many(batch)
        sample = batch['cpu_usage'][0]['sample_id']
        db.insert_many(batch)
        self.assertEqual(sample, batch['cpu_usage'][0]['sample_id'])
        self.assertIn('sample_id', db.calls[0][0])
        self.assertIn('address', db.calls[1][0])
        self.assertNotIn('sample_id', batch['tags'][0])

    def test_pre_migration_writer_does_not_require_sample_id(self):
        db = Writer(set())
        batch = {'cpu_usage': [{'timestamp': 'now', 'usage': 10}]}
        db.insert_many(batch)
        self.assertNotIn('sample_id', db.calls[0][0])
