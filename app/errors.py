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


class MediaTooLargeError(AppError):
    """Keep TOO_LARGE compatible while identifying media byte caps to delivery."""

    def __init__(self, message: str, permanent: bool = True):
        super().__init__("TOO_LARGE", message, permanent)
