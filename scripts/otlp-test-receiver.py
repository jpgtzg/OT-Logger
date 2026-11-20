#!/usr/bin/env python3
"""Local OTLP/HTTP JSON metrics sink for the Balcones pilot (stdlib only)."""
import argparse
import hashlib
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4318)
    parser.add_argument('--db', type=Path, default=Path('otlp-test-received.sqlite3'))
    args = parser.parse_args()
    os.umask(0o077)
    with sqlite3.connect(args.db) as db:
        db.execute('PRAGMA synchronous = FULL')
        db.execute('CREATE TABLE IF NOT EXISTS batches (digest TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        db.commit()

        class Receiver(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path != '/v1/metrics':
                    return self.reply(404, {'message': 'Use /v1/metrics'})
                if self.headers.get_content_type() != 'application/json' or self.headers.get('Content-Encoding', 'identity') != 'identity':
                    return self.reply(415, {'message': 'Only uncompressed OTLP JSON is supported'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1024 * 1024:
                        return self.reply(413, {'message': 'Batch must be between 1 byte and 1 MiB'})
                    payload = self.rfile.read(size)
                    if len(payload) != size:
                        raise ValueError('Incomplete request')
                    body = json.loads(payload)
                    if not isinstance(body, dict) or not isinstance(body.get('resourceMetrics'), list) or not body['resourceMetrics']:
                        raise ValueError('Expected nonempty resourceMetrics')
                except (ValueError, OSError) as exc:
                    return self.reply(400, {'message': str(exc)})
                digest = hashlib.sha256(payload).hexdigest()
                try:
                    with db:
                        inserted = db.execute('INSERT OR IGNORE INTO batches VALUES (?, ?)', (digest, payload)).rowcount
                except sqlite3.Error:
                    return self.reply(503, {'message': 'Could not persist batch'})
                print(f'{"saved" if inserted else "already saved"} {digest}', flush=True)
                self.reply(200, {})

        with HTTPServer(('127.0.0.1', args.port), Receiver) as server:
            print(f'Listening on http://127.0.0.1:{server.server_port}/v1/metrics; database: {args.db}', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
