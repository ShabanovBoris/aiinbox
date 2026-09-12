"""Машинно-читаемые коды ошибок системных границ (PRODUCT_SPEC §57).

Каждая ошибка границы несёт code: воркер пишет его в items.error_code,
пользователь получает понятное сообщение, стек остаётся в логах.
permanent=True означает «повтор бессмыслен» (4xx, security, TOO_LARGE).
"""


class AppError(Exception):
    def __init__(self, code: str, message: str, permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent
