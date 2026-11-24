"""Unit tests for bounded HTTP range reads and early rejection of non-206 responses."""
from __future__ import annotations

import httpx
import pytest

from ps1.range_reader import HTTPRangeFile, NonRangeResponseError


class _CountingStream(httpx.SyncByteStream):
    """Streaming body that records how many chunks/bytes were actually pulled."""

    def __init__(self, chunks: list[bytes], fail_if_read: bool = False):
        self._chunks = chunks
        self._fail_if_read = fail_if_read
        self.chunks_read = 0
        self.bytes_read = 0
        self.closed = False

    def __iter__(self):
        if self._fail_if_read:
            raise AssertionError("response body must not be consumed")
        for chunk in self._chunks:
            self.chunks_read += 1
            self.bytes_read += len(chunk)
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_non_206_response_rejected_without_consuming_body():
    body = _CountingStream([b"X" * 1024] * 100, fail_if_read=True)
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(16 * 1024**3)})
        get_calls += 1
        return httpx.Response(200, headers={"Content-Length": str(16 * 1024**3)}, stream=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = HTTPRangeFile("https://example.invalid/archive.7z", block=1024, retries=3, client=client)
    try:
        with pytest.raises(NonRangeResponseError, match="HTTP 200"):
            reader.read(64)
        assert body.bytes_read == 0
        assert body.chunks_read == 0
        assert body.closed is True
        assert get_calls == 1
    finally:
        reader.close()
        client.close()


def test_oversized_206_stream_stops_before_buffering_unbounded_body():
    # Server claims 206 without Content-Length and sends 100 chunks of 512 bytes for a 1024-byte block.
    body = _CountingStream([b"A" * 512] * 100)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "4096"})
        return httpx.Response(206, headers={"Content-Range": "bytes 0-1023/4096"}, stream=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = HTTPRangeFile(
        "https://example.invalid/archive.7z",
        block=1024,
        max_range_bytes=2048,
        retries=1,
        client=client,
    )
    try:
        with pytest.raises(OSError, match="exceeded requested 1024 bytes"):
            reader.read(16)
        # Stopped on the 3rd 512-byte chunk (1536 > 1024), nowhere near all 100 chunks
        assert body.chunks_read <= 3
        assert body.closed is True
    finally:
        reader.close()
        client.close()


def test_mismatched_content_length_and_content_range_rejected_before_body():
    body = _CountingStream([b"B" * 4096], fail_if_read=True)

    def bad_length_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "4096"})
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-1023/4096", "Content-Length": "4096"},
            stream=body,
        )

    client = httpx.Client(transport=httpx.MockTransport(bad_length_handler))
    reader = HTTPRangeFile(
        "https://example.invalid/archive.7z",
        block=1024,
        max_range_bytes=2048,
        retries=1,
        client=client,
    )
    try:
        with pytest.raises(OSError, match="Content-Length mismatch"):
            reader.read(16)
        assert body.bytes_read == 0
    finally:
        reader.close()
        client.close()

    body2 = _CountingStream([b"C" * 1024], fail_if_read=True)

    def bad_range_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "4096"})
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 500-1523/4096", "Content-Length": "1024"},
            stream=body2,
        )

    client2 = httpx.Client(transport=httpx.MockTransport(bad_range_handler))
    reader2 = HTTPRangeFile(
        "https://example.invalid/archive.7z",
        block=1024,
        max_range_bytes=2048,
        retries=1,
        client=client2,
    )
    try:
        with pytest.raises(OSError, match="unexpected Content-Range"):
            reader2.read(16)
        assert body2.bytes_read == 0
    finally:
        reader2.close()
        client2.close()


def test_unbounded_read_rejected_and_valid_206_reads_work():
    payload = bytes(i % 251 for i in range(4096))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(payload))})
        range_header = request.headers["Range"]
        assert range_header.startswith("bytes=")
        start_s, end_s = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_s), int(end_s)
        chunk = payload[start : end + 1]
        return httpx.Response(
            206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                "Content-Length": str(len(chunk)),
            },
            content=chunk,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = HTTPRangeFile(
        "https://example.invalid/archive.7z",
        block=512,
        max_range_bytes=1024,
        retries=1,
        client=client,
    )
    try:
        # Unbounded read(-1) on 4096-byte file exceeds max_range_bytes=1024
        with pytest.raises(OSError, match="refusing unbounded read"):
            reader.read(-1)

        # Bounded reads, seek, and readinto succeed cleanly
        assert reader.read(100) == payload[:100]
        reader.seek(2000)
        buf = bytearray(300)
        assert reader.readinto(buf) == 300
        assert bytes(buf) == payload[2000:2300]
    finally:
        reader.close()
        client.close()
