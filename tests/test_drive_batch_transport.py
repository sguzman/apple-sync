"""Socket-only regression tests: never contact Apple or start rclone."""
from __future__ import annotations

import json
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_transport as transport

ID = 'FOLDER::com.apple.CloudDocs::folder123'


def success_response():
    return {'result': [{
        'drivewsid': ID, 'status': 'OK', 'type': 'FOLDER',
        'directChildrenCount': 1,
        'items': [{'drivewsid': 'FILE::com.apple.CloudDocs::file123',
                   'type': 'FILE', 'name': 'private.txt', 'size': 7}],
    }]}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        first = self.rfile.readline()
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in (b'\r\n', b'\n', b''):
                break
            key, _, value = line.decode('ascii').partition(':')
            headers[key.lower()] = value.strip()
        raw = self.rfile.read(int(headers['content-length']))
        self.server.requests.append((first, json.loads(raw)))
        status, result = self.server.output
        payload = json.dumps(result).encode('utf-8')
        self.wfile.write(f'HTTP/1.1 {status} STATUS\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode('ascii') + payload)


class _Server(socketserver.UnixStreamServer):
    allow_reuse_address = True


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.dir.chmod(0o700)
        self.sock = self.dir / 'rc.sock'
        self.server = _Server(str(self.sock), _Handler)
        self.server.requests = []
        self.server.output = (200, success_response())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_private_json_post_and_validated_reply(self):
        response = transport.fetch_batch(self.sock, [('private/folder', ID)])
        self.assertEqual(response[0][0], 'private/folder')
        self.assertEqual(len(response[0][1]), 1)
        first, sent = self.server.requests[0]
        self.assertTrue(first.startswith(b'POST /backend/command HTTP/1.1'))
        self.assertEqual(sent, {'fs': 'icloud:', 'command': 'apple-sync-read-batch',
                                'arg': [ID], 'opt': {}})
        self.assertNotIn(b'folder123', first)

    def test_refuses_non_200_response_without_raw_error(self):
        self.server.output = (403, {'error': 'private folder token'})
        with self.assertRaises(transport.BatchTransportError) as caught:
            transport.fetch_batch(self.sock, [('private/folder', ID)])
        self.assertEqual(str(caught.exception), 'BATCH_RC_REFUSED')

    def test_refuses_malformed_backend_output(self):
        for response in ({'result': []}, {'error': 'private'}, {'result': 'secret'}):
            self.server.output = (200, response)
            with self.subTest(response=response), self.assertRaises(transport.BatchTransportError) as caught:
                transport.fetch_batch(self.sock, [('private/folder', ID)])
            self.assertNotIn('private', str(caught.exception))

    def test_invalid_request_never_contacts_socket(self):
        for batch in ([('private/folder', 'secret')],
                      [('private/folder', ID + '#etag')], [],
                      [('private/a', ID), ('private/b', ID)]):
            with self.subTest(batch=batch), self.assertRaises(transport.BatchTransportError):
                transport.fetch_batch(self.sock, batch)
        self.assertEqual(self.server.requests, [])

    def test_rejects_outside_socket_dir_permissions(self):
        self.dir.chmod(0o755)
        with self.assertRaisesRegex(transport.BatchTransportError, 'UNTRUSTED_BATCH_SOCKET'):
            transport.fetch_batch(self.sock, [('private/folder', ID)])
        self.assertEqual(self.server.requests, [])

    def test_rejects_timeout_outside_window(self):
        for timeout in (0, 31, True):
            with self.subTest(timeout=timeout), self.assertRaises(transport.BatchTransportError):
                transport.fetch_batch(self.sock, [('private/folder', ID)], timeout=timeout)
        self.assertEqual(self.server.requests, [])


if __name__ == '__main__':
    unittest.main()
