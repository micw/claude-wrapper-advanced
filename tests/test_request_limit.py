"""Unit tests for the HTTP request-body limit; no server, CLI or backend required."""
import json
import unittest

from fastapi import HTTPException

from app.config import settings
from app.main import _request_json


class FakeRequest:
    def __init__(self, chunks, content_length=None):
        self._chunks = chunks
        self.headers = ({"content-length": str(content_length)}
                        if content_length is not None else {})

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


class TestRequestLimit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._limit = settings.max_request_body_bytes
        settings.max_request_body_bytes = 32

    def tearDown(self):
        settings.max_request_body_bytes = self._limit

    async def test_json_below_limit_is_read(self):
        raw = json.dumps({"ok": True}).encode()
        self.assertEqual(await _request_json(FakeRequest([raw])), {"ok": True})

    async def test_content_length_rejects_before_reading(self):
        with self.assertRaises(HTTPException) as caught:
            await _request_json(FakeRequest([], content_length=33))
        self.assertEqual(caught.exception.status_code, 413)

    async def test_chunked_body_cannot_bypass_limit(self):
        with self.assertRaises(HTTPException) as caught:
            await _request_json(FakeRequest([b"{" + b" " * 20, b" " * 20 + b"}"]))
        self.assertEqual(caught.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
