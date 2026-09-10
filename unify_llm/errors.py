from __future__ import annotations


class ProxyError(Exception):
    def __init__(self, message: str, status_code: int = 502, detail: object | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class ConfigError(ProxyError):
    def __init__(self, message: str, detail: object | None = None):
        super().__init__(message, status_code=500, detail=detail)


class ModelNotFoundError(ProxyError):
    def __init__(self, model: str):
        super().__init__(f"Model not found: {model}", status_code=404)
        self.model = model


class UpstreamError(ProxyError):
    def __init__(self, message: str, status_code: int = 502, detail: object | None = None):
        super().__init__(message, status_code=status_code, detail=detail)
