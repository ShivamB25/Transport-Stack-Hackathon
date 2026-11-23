"""Minimal seekable HTTP range reader for extracting members of remote 7z files."""
from __future__ import annotations

import io
import time

import httpx

from ps1.config import ARCHIVE_USER_AGENT


class HTTPRangeFile(io.RawIOBase):
    """A read-only, seekable file backed by cached HTTP range requests."""

    def __init__(self, url: str, block: int = 8 << 20, retries: int = 5):
        self.url = url
        self.block = block
        self.retries = retries
        self.client = httpx.Client(
            headers={"User-Agent": ARCHIVE_USER_AGENT},
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
        )
        response = self.client.head(url)
        response.raise_for_status()
        self.size = int(response.headers["content-length"])
        self.pos = 0
        self.cache: dict[int, bytes] = {}
        self.nreq = 0
        self.nbytes = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        elif whence == 2:
            self.pos = self.size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        return self.pos

    def _get(self, start: int, end: int) -> bytes:
        for attempt in range(self.retries):
            try:
                response = self.client.get(
                    self.url, headers={"Range": f"bytes={start}-{end - 1}"}
                )
                if response.status_code != 206:
                    raise OSError(f"range request returned HTTP {response.status_code}")
                expected = end - start
                if len(response.content) != expected:
                    raise OSError(
                        f"short range response: got {len(response.content)}, expected {expected}"
                    )
                self.nreq += 1
                self.nbytes += len(response.content)
                return response.content
            except (httpx.HTTPError, OSError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        if n > 4 * self.block:
            data = self._get(self.pos, self.pos + n)
        else:
            out = bytearray()
            p = self.pos
            end = self.pos + n
            while p < end:
                block_number = p // self.block
                if block_number not in self.cache:
                    start = block_number * self.block
                    if len(self.cache) > 64:
                        self.cache.clear()
                    self.cache[block_number] = self._get(
                        start, min(start + self.block, self.size)
                    )
                block = self.cache[block_number]
                offset = p - block_number * self.block
                chunk = block[offset : offset + (end - p)]
                if not chunk:
                    raise OSError("empty range cache chunk")
                out += chunk
                p += len(chunk)
            data = bytes(out)
        self.pos += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self.client.close()
        super().close()
