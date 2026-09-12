from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.storage.models import Item


class TextExtractor:
    """Извлечение Phase 2: текст сообщения уже является содержимым.

    Специального фреймворка extractors нет — единый пайплайн получает
    NormalizedContent; web/voice extractor'ы добавляются в своих фазах.
    """

    async def extract(self, item: Item) -> NormalizedContent:
        return NormalizedContent(
            source_type=SourceType.TEXT,
            text=item.user_note,
        )
