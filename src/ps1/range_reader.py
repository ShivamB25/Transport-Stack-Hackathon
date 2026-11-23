"""Minimal seekable HTTP range reader for extracting members of remote 7z files."""
from __future__ import annotations

import io
import time

import httpx

from ps1.config import ARCHIVE_USER_AGENT

DEFAULT_BLOCK_BYTES = 8 << 20
DEFAULT_MAX_RANGE_BYTES = 32 << 20


class NonRangeResponseError(OSError):
    """Raised when a remote endpoint returns a non-206 response to a range request."""


class HTTPRangeFile(io.RawIOBase):
    """A read-only, seekable file backed by cached HTTP range requests."""

    def __init__(
        self,
        url: str,
        block: int = DEFAULT_BLOCK_BYTES,
        retries: int = 5,
        max_range_bytes: int = DEFAULT_MAX_RANGE_BYTES,
        client: httpx.Client | None = None,
    ):
        if block <= 0:
            raise ValueError(f"block must be positive: {block}")
        if max_range_bytes < block:
            raise ValueError(
                f"max_range_bytes ({max_range_bytes}) must be >= block ({block})"
            )
        self.url = url
        self.block = block
        self.retries = retries
        self.max_range_bytes = max_range_bytes
        self._owns_client = client is None
        self.client = client or httpx.Client(
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
        if start < 0 or end <= start or end > self.size:
            raise ValueError(f"invalid byte range [{start}, {end}) for size {self.size}")
        expected = end - start
        if expected > self.max_range_bytes:
            raise ValueError(
                f"requested range of {expected} bytes exceeds max_range_bytes={self.max_range_bytes}"
            )
        expected_range_prefix = f"bytes {start}-{end - 1}/"
        for attempt in range(self.retries):
            try:
                with self.client.stream(
                    "GET", self.url, headers={"Range": f"bytes={start}-{end - 1}"}
                ) as response:
                    if response.status_code != 206:
                        msg = (
                            f"range request returned HTTP {response.status_code}; "
                            "refusing non-206 response without reading body"
                        )
                        if response.status_code == 429 or response.status_code >= 500:
                            raise OSError(msg)
                        raise NonRangeResponseError(msg)

                    content_range = response.headers.get("content-range")
                    if content_range is not None and not content_range.lower().startswith(
                        expected_range_prefix
                    ):
                        raise OSError(
                            f"unexpected Content-Range {content_range!r}, "
                            f"expected prefix {expected_range_prefix!r}"
                        )

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError as exc:
                            raise OSError(
                                f"invalid Content-Length header: {content_length!r}"
                            ) from exc
                        if declared != expected:
                            raise OSError(
                                f"range Content-Length mismatch: got {declared}, expected {expected}"
                            )

                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        if len(buf) + len(chunk) > expected:
                            raise OSError(
                                f"range response exceeded requested {expected} bytes"
                            )
                        buf.extend(chunk)
                    if len(buf) != expected:
                        raise OSError(
                            f"short range response: got {len(buf)}, expected {expected}"
                        )
                    self.nreq += 1
                    self.nbytes += len(buf)
                    return bytes(buf)
            except NonRangeResponseError:
                raise
            except (httpx.HTTPError, OSError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def read(self, n: int = -1) -> bytes:
        remaining = max(0, self.size - self.pos)
        if remaining == 0:
            return b""
        if n is None or n < 0:
            if remaining > self.max_range_bytes:
                raise OSError(
                    f"refusing unbounded read of {remaining} bytes "
                    f"(max_range_bytes={self.max_range_bytes})"
                )
            n = remaining
        n = max(0, min(n, remaining))
        if n == 0:
            return b""
        if n > 4 * self.block:
            out = bytearray()
            p = self.pos
            end = self.pos + n
            while p < end:
                step_end = min(p + self.max_range_bytes, end)
                out += self._get(p, step_end)
                p = step_end
            data = bytes(out)
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
        if self._owns_client:
            self.client.close()
        super().close()
