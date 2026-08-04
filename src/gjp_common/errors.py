"""跨产品共享的安全业务错误。"""


class DomainError(ValueError):
    """领域错误：安全的、面向用户的确定性业务失败异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
