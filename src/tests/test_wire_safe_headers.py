"""Regression tests for non-ASCII header forwarding (see _wire_safe_headers).

A client may send a header value carrying non-ASCII bytes (e.g. a session id
derived from CJK text). uvicorn/ASGI decodes header bytes with latin-1, so
``request.headers`` exposes them as str with code points >127. The previous
``dict(request.headers)`` handed those to httpx, which ASCII-encodes header
values and raised UnicodeEncodeError -- a bare 500 before the request was sent.
"""

import pytest
from starlette.datastructures import Headers

from vllm_router.services.request_service.request import _wire_safe_headers


def _asgi_headers(raw_value: bytes) -> Headers:
    """Headers as ASGI delivers them: raw wire bytes, latin-1-decoded on access."""
    return Headers(
        raw=[
            (b"x-flow-conversation-id", raw_value),
            (b"content-type", b"application/json"),
        ]
    )


@pytest.mark.parametrize(
    "raw_value",
    (
        "中文测试".encode("utf-8"),  # CJK conversation id
        "conv-0123456789abcdef0123456789-中文".encode("utf-8"),  # ascii prefix + CJK
        "café-🚀".encode("utf-8"),  # latin-1 char + emoji (astral)
    ),
)
def test_wire_safe_headers_roundtrips_non_ascii_to_wire_bytes(raw_value: bytes) -> None:
    result = _wire_safe_headers(_asgi_headers(raw_value))
    # latin-1 round-trip reproduces the exact bytes the client put on the wire.
    assert result["x-flow-conversation-id"] == raw_value
    assert result["content-type"] == b"application/json"
    # bytes values are forwarded by httpx verbatim (no ASCII re-encode).
    assert all(isinstance(v, bytes) for v in result.values())


def test_wire_safe_headers_does_not_raise_where_dict_would() -> None:
    headers = _asgi_headers("客户端浏览器".encode("utf-8"))
    # The crashing path was value.encode("ascii"); assert it would have failed...
    with pytest.raises(UnicodeEncodeError):
        headers["x-flow-conversation-id"].encode("ascii")
    # ...while the fix encodes the same value without raising.
    _wire_safe_headers(headers)
