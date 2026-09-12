import enum


# Техническое состояние конвейера обработки — не смешивается с пользовательским
# жизненным циклом (см. PRODUCT_SPEC §10).
class ProcessingStatus(enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class ItemState(enum.Enum):
    ACTIVE = "ACTIVE"
    SNOOZED = "SNOOZED"
    DONE = "DONE"
    ARCHIVED = "ARCHIVED"


# Источник содержимого. Расширяется по фазам (WEB — Phase 3, VOICE/AUDIO — Phase 5,
# YOUTUBE — Phase 6); после NormalizedContent все источники идут по одному пайплайну.
class SourceType(enum.Enum):
    TEXT = "TEXT"
    WEB = "WEB"
    VOICE = "VOICE"
    AUDIO = "AUDIO"


# Тип контента определяет, попадает ли Item в /today и что с ним делать;
# категория при этом остаётся динамической строкой (PRODUCT_SPEC §11–12).
class ItemType(enum.Enum):
    ACTION = "ACTION"
    LEARN = "LEARN"
    READ = "READ"
    WATCH = "WATCH"
    IDEA = "IDEA"
    REFERENCE = "REFERENCE"
    SOMEDAY = "SOMEDAY"


# Вид содержимого в contents (PRODUCT_SPEC §48): длинный контент хранится
# отдельно от Item и переживает restart (ТЗ §46).
class ContentKind(enum.Enum):
    USER_TEXT = "USER_TEXT"
    WEB_TEXT = "WEB_TEXT"
    TRANSCRIPT = "TRANSCRIPT"
    VISUAL_NOTES = "VISUAL_NOTES"
    DESCRIPTION = "DESCRIPTION"
    CHUNK_SUMMARY = "CHUNK_SUMMARY"
