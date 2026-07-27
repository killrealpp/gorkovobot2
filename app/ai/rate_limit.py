from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

# Простая in-memory реализация rate limiting
class RateLimiter:
    def __init__(self, max_requests: int = 100, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str = "default") -> bool:
        """Проверяет, не превышен ли лимит. Возвращает True если лимит превышен."""
        now = time.time()
        # Очищаем старые запросы
        self.requests[key] = [t for t in self.requests[key] if now - t < self.time_window]

        if len(self.requests[key]) >= self.max_requests:
            return True

        self.requests[key].append(now)
        return False

# Глобальный экземпляр
_rate_limiter = RateLimiter()

def is_llm_rate_limited(key: Optional[str] = None) -> bool:
    """Проверяет, не превышен ли лимит запросов к LLM."""
    return _rate_limiter.check(key or "default")
