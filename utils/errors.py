# utils/errors.py
from __future__ import annotations
from typing import Any, Optional

class BaseTerminalError(Exception):
    def __init__(self, message: str, *, code: Optional[str] = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload

class ConfigError(BaseTerminalError): ...
class DataFormatError(BaseTerminalError): ...
class RateLimitError(BaseTerminalError): ...
class ExchangeError(BaseTerminalError): ...
class AuthError(BaseTerminalError): ...
class NetworkError(BaseTerminalError): ...

_OKX_CODE_MAP = {
    '0': None,
    '50113': AuthError,
    '50114': AuthError,
    '50115': AuthError,
    '51113': RateLimitError,
    '1013':  RateLimitError,
}

def raise_for_okx(code: str, msg: str, payload: Any = None):
    if str(code) == '0':
        return
    exc_cls = _OKX_CODE_MAP.get(str(code), ExchangeError)
    raise exc_cls(f'OKX[{code}] {msg}', code=str(code), payload=payload)

def is_rate_limit(err: Exception) -> bool:
    return isinstance(err, RateLimitError)
