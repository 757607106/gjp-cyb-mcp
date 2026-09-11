"""跨产品共享的安全业务错误。"""


class DomainError(ValueError):
    """领域错误：安全的、面向用户的确定性业务失败异常。"""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message

        self.details = dict(details or {})

    def as_dict(self) -> dict:
        """统一序列化安全错误；仅由适配器加入白名单诊断字段。"""
        result = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result
