"""
Модуль фильтрации сообщений по ключевым словам пользователя.
"""
import logging

logger = logging.getLogger(__name__)


class AdFilter:
    """
    Фильтрует сообщения по пользовательским правилам.

    Правило может содержать несколько слов через '+' — срабатывает
    только если ВСЕ части присутствуют в тексте.

    Примеры:
      "промокод"               → фильтровать если есть «промокод»
      "скидка+купить"          → фильтровать только если есть оба слова
    """

    def _check_keywords(self, text: str, keywords: list[str]) -> tuple[bool, str]:
        text_lower = text.lower()
        for rule in keywords:
            parts = [p.strip() for p in rule.split("+") if p.strip()]
            if all(part.lower() in text_lower for part in parts):
                label = " + ".join(parts) if len(parts) > 1 else parts[0]
                return True, f"Найдено правило: '{label}'"
        return False, ""

    async def is_ad(
        self,
        text: str,
        keywords: list[str],
        use_ai: bool = False,
    ) -> tuple[bool, str]:
        if not text or not text.strip():
            return False, ""
        return self._check_keywords(text, keywords)


# Глобальный экземпляр фильтра
ad_filter = AdFilter()
