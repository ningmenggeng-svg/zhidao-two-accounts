"""Safe response metadata, without response bodies or authentication data."""


class PlatformResponseError(ValueError):
    def __init__(self, reason, *, http_status=None, code=None):
        self.reason = reason
        self.http_status = http_status
        # Only numeric status codes may enter diagnostics; never retain free text.
        self.code = (
            int(code)
            if isinstance(code, (int, str)) and not isinstance(code, bool) and str(code).lstrip("-").isdigit()
            else None
        )
        super().__init__(f"Zhihuishu response error: {reason}")
