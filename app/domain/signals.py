"""Deterministic evidence signals shared by prompt construction and result checks.

These helpers only recognise what the captured evidence explicitly states. They
never decide the answer, invent a conclusion, or rewrite measured content.
"""

import re
import sys
from bisect import bisect_right
from collections.abc import Mapping

_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})
_VERDICT_ACTIONS = r"pick|choose|select|declare|establish|determine|produce|identify|name"
# A Russian or Ukrainian genitive after "победитель/переможець" names the event the
# person won, not the side of a method comparison: "победителя тендера" is a
# business outcome, "победителя сравнения/метода" is the compared side. Defined
# before the verdict expressions that guard against those events.
_NON_OUTCOME_WIN_OBJECTS_CYRILLIC = (
    r"тендер\w*|выбор(?:ы|ов|ам|ами|ах)|конкурс\w*|закуп\w*|аукцион\w*|"
    r"викторин\w*|преми\w*|лотере\w*|грант(?:а|ов|ам|ами|ах)|"
    r"вибор(?:и|ів|ам|ами|ах)|закупівл\w*|аукціон\w*|вікторин\w*|премі\w*|"
    r"лотере\w*|грант(?:а|ів|ам|ами|ах)"
)
# An election, tender, auction or quiz has participants and one winner; a method
# comparison has sides. A no-winner phrase framed by such an event ("no winner was
# announced in the election", "в рамках тендера победителя нет") reports that
# event's outcome, not an unresolved comparison, so a later named event win
# ("Smith won the election") must not leave a no-winner verdict on the title.
_NON_OUTCOME_EVENTS = (
    r"elections?|tenders?|auctions?|contests?|competitions?|quiz(?:zes)?|raffles?|"
    r"lotter(?:y|ies)|awards?|medals?|troph(?:y|ies)|scholarships?|grants?|races?|"
    r"votes?|bids?|bidding|seats?|"
    rf"{_NON_OUTCOME_WIN_OBJECTS_CYRILLIC}"
)
_EVENT_NOUN = rf"(?:{_NON_OUTCOME_EVENTS})\b"
_WIN_NOUN = r"(?:no\s+winner|winners?|побед\w*|перемож\w*)"
# The event frames the phrase from either side: "no winner … in the election",
# "в рамках тендера победителя нет", "the election winner", "победителя тендера".
_EVENT_SCOPE_PREPOSITION = (
    r"(?:in|at|of|for|during|after|before|from|into|within|"
    r"в|во|на|среди|серед|после|після|в\s+рамках|у\s+рамках|в\s+ходе|у\s+ході|"
    r"по\s+итогам|по\s+результатам|за\s+підсумками|за\s+результатами|до|от)"
)
_EVENT_VERDICT_FRAME = re.compile(
    r"(?i)"
    rf"\b{_WIN_NOUN}\b(?:\s+\S+){{0,5}}?\s+{_EVENT_SCOPE_PREPOSITION}\s+"
    rf"(?:the\s+)?(?:\w+\s+){{0,2}}?{_EVENT_NOUN}"
    rf"|\b{_EVENT_NOUN}(?:\s+\S+){{0,4}}?\s+\b{_WIN_NOUN}\b"
)
# "won praise", "won the contract", "won the office quiz" and "outperformed
# expectations" report attention, business or a forecast beat, not which alternative
# measured better. Both the bare and the adverbised forms need the same lookahead.
_NON_OUTCOME_WIN_OBJECTS = (
    r"praise|awards?|prizes?|prize\s+money|recognition|attention|acclaim|support|"
    r"approval|backing|funding|contracts?|deals?|bids?|tenders?|auctions?|races?|"
    r"elections?|votes?|seats?|"
    r"traction|users?|customers?|market\s+share|hearts|fans|admirers|bidding|"
    r"quiz(?:zes)?|contests?|competitions?|raffles?|lotter(?:y|ies)|medals?|"
    r"trophies?|scholarships?|grants?|publicity|headlines"
)
_NON_OUTCOME_WIN = rf"(?!\s+(?:the\s+|a\s+|an\s+)?(?:[\w-]+\s+)?(?:{_NON_OUTCOME_WIN_OBJECTS})\b)"
# A between/между phrase is a comparison only when it names the compared sides, an
# optional label ("option A and option B", "методы 1 и 2") included. A decision
# between actions ("between migrating and staying") and a relationship ("between
# price and demand", "зависимость между ценой и спросом") are not comparisons of
# alternatives, so they must not become a no-winner verdict.
_COMPARISON_SIDES = (
    r"(?:approach(?:es)?|methods?|models?|options?|variants?|configurations?|"
    r"baselines?|agents?|prompts?|systems?|tools?|algorithms?|techniques?|"
    r"strategies?|pipelines?|implementations?|solutions?|architectures?|"
    r"метод\w*|подход\w*|вариант\w*|модел\w*|алгоритм\w*|систем\w*|"
    r"инструмент\w*|конфигурац\w*|техник\w*|стратег\w*|реализац\w*|"
    r"архитектур\w*|варіант\w*|підхід\w*|модел\w*|алгоритм\w*|систем\w*|"
    r"інструмент\w*|конфігурац\w*|технік\w*|стратег\w*|реалізац\w*|"
    r"архітектур\w*)"
)
# A sentence that names a compared side is a comparison verdict even when it also
# mentions an event ("neither method won the tender").
_COMPARISON_SIDE_NAMES = re.compile(rf"(?i)\b(?:{_COMPARISON_SIDES})\b")
# Naming a side noun is not by itself evidence of a comparison: "the tender for the
# new monitoring system" names the event's object, and "the system was expensive"
# describes it. The side only outranks the event frame when its own clause actually
# compares sides: a comparison predicate follows it ("two systems differed", "both
# methods performed equally") or a comparison frame introduces it ("comparison of
# the two methods").
# "better than expected" grades one outcome against a baseline the source never
# compared: it says nothing about which side won, so it must not turn an event
# verdict ("the tender has no winner") into a comparison verdict. The baseline list
# is closed on purpose: "better than the previous release" compares two sides and
# still counts.
# The baseline may be separated from the comparative by a punctuation comma
# ("better, than expected", "лучше, чем ожидалось") and may carry an article
# ("better than the expected 20 ms"), so it is matched from the comparative's end
# in the whole sentence instead of inside a clause window.
# A baseline may also follow the comparative without a conjunction: the Russian
# genitive ("лучше ожидаемого", "лучше плана") and the English "outperformed
# expectations" grade one outcome against a reference the source never compared, so
# neither names a compared side. The English list mirrors _NON_OUTCOME_OUTPERFORM,
# which already excludes these complements on the win path.
_RU_EXPECTATION_BASELINE = (
    r"(?:ожидаем\w*|ожидани\w*|прогноз(?:а|у|ов)?|прогнозируем\w*|"
    r"прогнозировавш\w*)"
)
_RU_ALTERNATIVE_BASELINE = r"план(?:а|у|ов|ы)?"
_EN_EXPECTATION_BASELINE = (
    r"(?:(?:the\s+)?(?:expectations?|estimates?|forecasts?|projections?|consensus|"
    r"targets?|markets?|peers?|industry)|benchmarks?\s+of)"
)
_EN_ALTERNATIVE_BASELINE = r"(?:the\s+)?plans?"
# A baseline that carries a designator names a compared side: "model A was better than
# plan B" and "модель A лучше плана Б" compare two plans, so a no-winner sentence
# about them is a comparison verdict. A designator names one alternative — a single
# letter ("plan B", "плана Б") or a small ordinal ("plan 2", "plans B and C"). Case
# separates a label from prose only in mixed-case writing, where "plan A"/"плана В по
# задержке" write the label uppercase and the prose lowercase; the lowercase
# homographs are the English article and the one-letter Russian and Ukrainian
# prepositions and conjunctions ("plan a year ago", "плана и по цене", "плана с учётом
# сезонности", "плана в цьому кварталі"). In case-normalized evidence (ALL CAPS or
# title case) the case distinguishes nothing, so "PLAN A YEAR AGO", "Plan A Year Ago"
# and "ПЛАНА И ПРОГНОЗА" read the same letter as prose; a letter that is not one of
# those words stays a label in either case ("plan B", "PLAN B", "plan b", "плана Б"),
# and so does "#2"/"№2". A measured value is excluded by shape: a number carrying a
# unit or a percent sign is a quantity ("the target 20 ms", "лучше плана 20
# процентов"), and so is a value written with a decimal part ("20.5 percent", "2,5
# мс"), a digit-grouped thousands separator ("1,200 ms", "1 200 мс") or a range in any
# spelling ("20–25", "20 – 25", "20‑25", "20−25", "20/25", "20 to 25 percent"), because
# the integer part of a measurement is not an ordinal label. A four-digit year is a
# time reference ("лучше прогноза в 2024 году"). "No. 2" is left out because the
# abbreviation also ends a sentence, so the detector would read the truncated sentence
# instead of the comparison (COMPAT-38-03 boundary). A full word after the baseline
# stays prose ("than expected revenue").
#
# Lowercase words that share the shape of a one-letter label. "і"/"й" (uk) and
# "у"/"о"/"к"/"с"/"в" (ru/uk) are prepositions or conjunctions, "и"/"а"/"я" are a
# conjunction, a conjunction and a pronoun, and "a" is the English article.
_LABEL_SERVICE_WORDS = frozenset({"a", "в", "и", "а", "я", "с", "к", "у", "о", "і", "й"})
_LABEL_QUANTITY = (
    r"(?:%|percent\w*|percentage|процент\w*|п\.\s?п\.|ms|msec|мс|secs?|seconds?|"
    r"сек\w*|minutes?|мин\w*|hours?|час\w*|days?|дн\w*|weeks?|недел\w*|months?|"
    r"мес\w*|years?|год\w*|лет|gb|mb|tb|кб|мб|гб|тб|points?|пункт\w*|times|раз)"
)
_LABEL_QUANTITY_WORD = re.compile(rf"(?i)\s*{_LABEL_QUANTITY}(?![\w-])")
# Unicode hyphens, the minus sign and the soft hyphen all write a range, so any of
# them between two numbers makes the token a measured span instead of a label.
_LABEL_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad"
_LABEL_SEPARATOR = re.compile(r"\s*-?\s*")
_LABEL_EXPLICIT_NUMBER = re.compile(r"[#№]\s?\d{1,3}(?![\w-])")
_LABEL_NUMBER = re.compile(r"\d{1,3}(?![\w-])")
_LABEL_LETTER = re.compile(r"[^\W\d_](?![\w-])")
# A value that continues past the integer: a decimal part, a digit-grouped thousands
# separator, a further number, or a range with a dash, a slash, "to" or "through".
_LABEL_VALUE_SHAPE = re.compile(
    rf"(?:[.,]\d|\s*\d|\s*(?:[{_LABEL_DASHES}/]|to\b|through\b)\s*\d)", re.IGNORECASE
)
# A baseline may also follow the comparative with a conjunction, in the singular or
# the plural/noun form ("better than expected", "better than expectations",
# "лучше, чем ожидания"). The noun lists below are shared with the no-conjunction
# branch so the two spellings cannot drift apart. A plan-like baseline is captured,
# because only it can name an alternative; its designator is read from the text after
# the match, where the baseline's own case is known.
_EXPECTATION_BASELINE = re.compile(
    r"(?i)(?:"
    r"(?:,?(?:\s+|\s*-\s*)than(?:\s+|\s*-\s*)|,?\s+(?:чем|ніж)\s+|,?\s+)"
    rf"(?P<alternative>{_EN_ALTERNATIVE_BASELINE}|{_RU_ALTERNATIVE_BASELINE})\b"
    r"|,?(?:\s+|\s*-\s*)than(?:\s+|\s*-\s*)(?:the\s+)?(?:expected|anticipated|"
    r"predicted|forecast|planned|projected|budgeted|usual|previously|ever|before)\b"
    rf"|,?(?:\s+|\s*-\s*)than\s+{_EN_EXPECTATION_BASELINE}\b"
    r"|,?\s+(?:чем|ніж)\s+(?:ожидал\w*|очікувал\w*|прогноз\w*|планировал\w*|"
    rf"{_RU_EXPECTATION_BASELINE})\b"
    rf"|,?\s+{_RU_EXPECTATION_BASELINE}\b"
    rf"|,?\s+{_EN_EXPECTATION_BASELINE}\b)"
)
_COMPARATIVE_SIDE_CUE = r"(?:better|worse|faster|slower|лучше|хуже|быстрее|медленнее)"
# "outperformed expectations" grades an outcome exactly as "better than expected"
# does, so it belongs to the graded cues whose complement is checked for a baseline.
_GRADED_SIDE_CUE = rf"(?:{_COMPARATIVE_SIDE_CUE}|outperform\w*)"
_SIDE_CUE_AFTER = re.compile(
    r"(?i)^[^.;:]{0,48}?\b(?:differ\w*|equally|superior|inferior|"
    r"trade-?offs?|различа\w*|отлича\w*|равн\w*|переваг\w*|"
    rf"(?P<graded>{_GRADED_SIDE_CUE}))\b"
)
_SIDE_CUE_BEFORE = re.compile(
    r"(?i)\b(?:compar\w*|versus|vs\.?|between|сравн\w*|между|порівнян\w*)\b"
    r"[^.;:]{0,40}$"
)
# An explicit inconclusive verdict in either word order. A bare "inconclusive"
# counts only as a label/standalone verdict, so explanatory prose such as "the
# article explains why comparisons can be inconclusive" does not claim a result.
_INCONCLUSIVE_OUTCOME = re.compile(
    r"(?i)(?:"
    rf"\b(?:did|do|does|could|can|was|were|unable|failed)\s+(?:not\s+|to\s+)"
    rf"(?:{_VERDICT_ACTIONS})\s+(?:a\s+|the\s+|any\s+)?"
    r"(?:clear\s+|single\s+|overall\s+|definitive\s+)?winner\b"
    # Passive order: "A winner could not be determined."
    r"|\b(?:a\s+|the\s+|any\s+)?winner\s+(?:could|can|would|might)\s+not\s+be\s+"
    r"(?:determined|established|chosen|declared|selected|picked|identified|named)\b"
    r"|\bwinner\s+(?:(?:was|is|were|are|remains?|remained)\s+)?"
    r"(?:not\s+(?:determined|established|chosen|declared|selected|picked|"
    r"identified|named)|undetermined|undecided|unresolved)\b"
    r"|\bno\s+(?:clear\s+|single\s+|overall\s+|definitive\s+)?winner\b"
    # "The winner was never announced" states the absence of a verdict, not a win.
    r"|\b(?:a\s+|the\s+)?winner\s+(?:was|is|has\s+been)?\s+never\s+"
    r"(?:announced|declared|determined|established|chosen|selected|named|"
    r"identified|picked)\b"
    r"|\b(?:results?|findings?|comparisons?|outcomes?|verdicts?|trials?|"
    r"studies|study|analyses|analysis|runs?|rounds?|tests?|attempts?|"
    r"experiments?|pilots?|evaluations?|replications?)"
    r"\b[^.;]{0,30}?\b(?:is|was|were|are|remains?|remained|seemed?|proved?)\s+"
    r"(?:\w+\s+){0,2}inconclusive\b"
    r"|\binconclusive\s+(?:results?|findings?|outcomes?|comparisons?|verdicts?)\b"
    r"|(?:^|(?<=[.!?]\s))inconclusive\b"
    # "mixed with legacy data" describes combining datasets, not a verdict.
    r"|\b(?:results?|findings?|comparisons?|outcomes?|verdicts?)\s+"
    r"(?:were|are|was|is|remained?|remains?)\s+mixed\b(?!\s+(?:with|into|in)\b)"
    r"|\bmixed\s+results?\b"
    # A null result is an explicit no-winner statement even without "winner".
    r"|\bno\s+(?:meaningful|significant|clear|measurable|statistically\s+significant)\s+"
    r"(?:difference|improvement|advantage)\b"
    r"|\bno\s+difference\b"
    r"|\bno\s+(?:of\s+)?(?:the\s+)?(?:\w+\s+){0,2}?"
    r"(?:method|approach|model|option|variant|system|tool|strategy|algorithm|"
    r"technique|configuration|baseline|run|trial|test|result|condition)s?\s+"
    r"(?:was|is|were|are|performed|did|fared)\s+"
    r"(?:clearly\s+|significantly\s+)?(?:better|superior|best)\b"
    r"|\b(?:comparisons?|outcomes?|verdicts?|results?)\b[^.;]{0,30}?"
    r"\b(?:remains?|was|is)\s+(?:an?\s+)?(?:open|unsettled|undecided|unresolved)"
    r"(?![\w-]|\s+(?:source|sources|ended|ending|access|data|standards?|format|"
    r"letter)\b)\b"
    # A named comparison that stays open is an explicit no-winner statement. A
    # bare "question remained unresolved" is unrelated uncertainty unless the
    # sentence actually names the thing being compared.
    r"|\b(?:comparison|verdict)\b[^.;]{0,30}?"
    r"\b(?:remained?|remains?|stayed?|stays?|was|is|were|are)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\b(?:question|issue|matter)\b[^.;]{0,80}?"
    r"\b(?:whether|which|who)\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|winner|outperform\w*|best|faster|"
    r"compare\w*)\b[^.;]{0,40}?"
    r"\b(?:remained?|remains?|stayed?|stays?|was|is|were|are)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\bwhether\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|winner|outperform\w*|best|faster)\b"
    r"[^.;]{0,40}?\b(?:remains?|remained?|stays?|stayed?|is|was)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\b(?:current|latest|recent|follow-up|second|confirmatory)\s+"
    r"(?:replication|run|study|trial|experiment|test|attempt|evaluation|analysis)\b"
    r"[^.;]{0,30}?\b(?:was|is|remained?|remains?|proved|seemed)\s+"
    r"(?:\w+\s+){0,2}inconclusive\b"
    r"|\bneither\s+(?:of\s+)?(?:the\s+|these\s+|those\s+|both\s+)?(?:\w+\s+){0,2}?"
    r"(?:was|is|were|are|proved|emerged)\s+"
    r"(?:clearly\s+|significantly\s+|measurably\s+|consistently\s+)?"
    r"(?:better|superior|ahead)\b"
    # "Neither method won" and "Both methods performed equally well" state the
    # same no-winner verdict without the word "winner". The subject must be a
    # compared side: an election, tender or quiz has participants, and "neither
    # vendor won the contract" names an event, not a comparison result.
    r"|\bneither\s+(?:of\s+)?(?:the\s+)?(?:two\s+)?"
    rf"(?:{_COMPARISON_SIDES}|them)\b(?:\s+\w+){{0,2}}?\s+(?:won|wins)\b"
    rf"{_NON_OUTCOME_WIN}"
    r"|\b(?:both|the\s+two)\s+(?:of\s+)?(?:the\s+)?"
    rf"(?:{_COMPARISON_SIDES}|them)\b(?:\s+\w+){{0,2}}?\s+"
    r"(?:performed|fared|did|scored|came\s+out)\s+(?:about\s+|approximately\s+|"
    r"roughly\s+|nearly\s+|almost\s+|essentially\s+)?equally\s+"
    r"(?:well|good|strong|fast|accurate|reliable)\b"
    # An open "which is better" question is a comparison verdict; "unclear which
    # GPU the pilot used" is unrelated uncertainty.
    r"|\bunclear\s+(?:which|who|whose)\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|faster|slower|wins?|won|winner|"
    r"outperform\w*|best)\b"
    r"|\bevenly\s+matched\b|\btoo\s+close\s+to\s+call\b"
    # Russian forms after ё folding. The passive admits an auxiliary ("не был
    # определён") and a named comparison between the noun and the negation
    # ("победитель сравнения так и не был определён").
    r"|\bпобедител\w*\b"
    rf"(?![^.;]{{0,30}}?\b(?:{_NON_OUTCOME_WIN_OBJECTS_CYRILLIC})\b)"
    r"[^.;]{0,30}?\b(?:так\s+и\s+)?не\s+"
    r"(?:(?:был|была|было|были|будет)\s+)?"
    r"(?:выбран|определен|установлен|найден|назван|выявлен)\w*\b"
    r"|\b(?:\w+\s+){0,2}?победител\w*"
    rf"(?![^.;]{{0,30}}?\b(?:{_NON_OUTCOME_WIN_OBJECTS_CYRILLIC})\b)"
    r"\s+(?:так\s+и\s+)?нет\b"
    r"|\b(?:сравнени\w*|анализ\w*|тест\w*|эксперимент\w*|исследовани\w*|"
    r"обзор\w*|бенчмарк\w*|оценк\w*|прогон\w*)\b[^.;]{0,40}?\bне\s+"
    r"(?:выявил\w*|показал\w*|определил\w*|установил\w*|назвал\w*|выбрал\w*)\s+"
    r"(?:явного\s+|единого\s+|общего\s+|лучшего\s+)?победител\w*\b"
    r"|\bне\s+удалось\s+(?:выявить|выбрать|определить|установить|назвать|найти)\s+"
    r"(?:явного\s+|единого\s+|общего\s+)?(?:победител\w*|"
    r"лучш\w+\s+(?:подход|метод|вариант|модел\w*|инструмент|алгоритм|"
    r"систем\w*|конфигураци)\w*)\b"
    # The reflexive paradigm needs every gender/number form: "оказались
    # смешанными", "оказалось неопределённым", "оказались противоречивыми".
    r"|\b(?:результат\w*|вывод\w*|сравнени\w*|итог\w*)\b[^.;]{0,40}?"
    r"\b(?:оказал(?:ся|ась|ось|ись)|остал(?:ся|ась|ось|ись)|стал(?:а|о|и)?|"
    r"был\w*|были)?\s*(?:смешанн\w*|неопределенн\w*|противоречив\w*)\b"
    r"|\b(?:смешанн\w*|неопределенн\w*|противоречив\w*)\s+результат\w*\b"
    # A bare predicate needs a comparison subject ("the forecast was uncertain"
    # is unrelated uncertainty, not a verdict about two alternatives).
    r"|\b(?:результат\w*|вывод\w*|итог\w*|исход\w*|сравнени\w*|выбор\w*)\b"
    r"[^.;]{0,40}?\b(?:остает\w*ся|остает\w*|остал\w*|был\w*|были|являет\w*ся)\s+"
    r"неопределенн\w*\b"
    # An open question and an undecided comparison in Russian and Ukrainian.
    r"|\bвопрос\w*\s+(?:остал\w*ся|остает\w*ся|остается|был\w*|являет\w*ся)\s+"
    r"(?:открыт\w*|нерешенн\w*|неразрешенн\w*)"
    r"|\b(?:неясно|непонятно),?\s+(?:какой|какая|какое|кто|что)\b"
    # Two sides with equal results are a no-winner statement even without the
    # word "winner": "Оба подхода показали примерно равные результаты".
    r"|\b(?:оба|обе)\b[^.;]{0,40}?(?:\b(?:показал\w*|оказал\w*|были|был\w*|"
    r"получил\w*|имел\w*)\s*)?"
    r"(?:примерно\s+|практически\s+|почти\s+)?равн\w*(?:\s+результат\w*)?\b"
    r"|\bпитання\s+(?:залишил\w*ся|залишаєть\w*ся|бул\w*)\s+відкрит\w*"
    r"|\b(?:незрозуміло|неясно),?\s+(?:який|яка|яке|хто|що)\b"
    # German and Spanish forms so a non-English verdict still reaches the guard.
    r"|\bkein(?:e|er)?\s+(?:klare[rn]?\s+)?(?:sieger|gewinner)\b"
    r"|\b(?:sieger|gewinner)\s+(?:konnte|lies|ließ)\s+(?:sich\s+)?nicht\s+"
    r"(?:ermittelt|bestimmt|gew(?:ae|ä)hlt|gewahlt)\s+werden\b"
    r"|\bergebnisse?\s+(?:waren|sind)\s+(?:gemischt|uneindeutig)\b"
    r"|\bno\s+se\s+(?:pudo|podria|podría)\s+(?:determinar|establecer)\s+"
    r"(?:un\s+)?ganador\b"
    r"|\bsin\s+(?:un\s+)?ganador\b"
    r"|\bresultados\s+(?:fueron|son)\s+mixtos\b"
    # Ukrainian forms: "Переможця не визначено", "результати були невизначеними".
    r"|\bперемож\w*\s+(?:так\s+і\s+)?не\s+"
    r"(?:(?:був|була|було|були)\s+)?"
    r"(?:визнач|обра|встанов|назва)\w*\b"
    r"|\bне\s+вдалося\s+(?:визначити|обрати|встановити|назвати)\s+переможця\b"
    r"|\b(?:результат\w*|виснов\w*|порівнян\w*)\s+"
    r"(?:були|був|була|було|є|залишил\w*ся)?\s*"
    r"(?:невизначен\w*|суперечлив\w*|змішан\w*)\b"
    r"|\b(?:невизначен\w*|суперечлив\w*|змішан\w*)\s+результат\w*\b"
    r"|\bжодн\w*\s+перемож\w*\s+не\s+(?:був|була|було|були)?\s*"
    r"(?:визнач|обра|встанов|назва)\w*\b"
    r")"
)
# The deterministic repair must write the verdict in the actual response
# language. A missing translation is better than an English suffix inside a
# German, Spanish, or Japanese title.
_INCONCLUSIVE_SUFFIX = {
    "en": "; no winner was established",
    "ru": "; победитель не определён",
    "uk": "; переможця не визначено",
    "de": "; kein Sieger wurde ermittelt",
    "es": "; no se determinó un ganador",
    "fr": "; aucun gagnant n'a été établi",
    "it": "; nessun vincitore è stato stabilito",
    "pt": "; nenhum vencedor foi determinado",
    "nl": "; geen winnaar werd bepaald",
    "pl": "; nie wyłoniono zwycięzcy",
    "tr": "; kazanan belirlenmedi",
}
# One claim boundary is shared by the no-winner detector, the reversal classifier,
# and the evaluation oracle. A sentence pronoun ("it", "this result") may carry the
# previous claim, but a coordinator starts a new claim with its own subject.
CLAIM_CLAUSE_BREAK = re.compile(
    r"(?i)[.;:,!?]|\b(?:but|however|although|though|whereas|yet|and|or|while|"
    r"но|однако|а|и|или)\b"
)


# A claim clause is a sentence fragment, so the scan is bounded: a verdict-heavy
# document must stay linear instead of rescanning everything before each match.
_CLAUSE_WINDOW = 400


def clause_before(text: str, index: int) -> str:
    """Return the claim clause ending at ``index`` using the shared boundary."""
    prefix = text[max(0, index - _CLAUSE_WINDOW) : index]
    breaks = list(CLAIM_CLAUSE_BREAK.finditer(prefix))
    return prefix[breaks[-1].end() :] if breaks else prefix


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_UKRAINIAN = re.compile(
    r"(?i)[ІіЇїЄєҐґ]|\b(?:перемож\w*|визнач\w*|невизнач\w*|порівнян\w*|"
    r"жодн\w*|суперечлив\w*|змішан\w*)\b"
)
# When no profile/content language is supplied, the deterministic suffix must not
# assume that every Latin-script title is English. These markers only select a
# translation; they never rewrite the title itself.
_GERMAN = re.compile(
    r"(?i)\b(?:der|die|das|und|kein\w*|wurde|wurden|waren|war|ist|sind|"
    r"ergebnis\w*|sieger\w*|gewinner\w*|gemischt|vergleich\w*|ermittelt|"
    r"bestimmt)\b"
)
_SPANISH = re.compile(
    r"(?i)\b(?:el|la|los|las|resultados?|ganador\w*|fueron|son|comparación|"
    r"mixtos|determinó)\b"
)
_FRENCH = re.compile(
    r"(?i)\b(?:les?|des?|résultats?|gagnant\w*|aucun\w*|ont|était|étaient|"
    r"comparaison|établi)\b"
)
_ENGLISH = re.compile(
    r"(?i)\b(?:the|and|but|was|were|are|is|no|not|winner|results?|comparison|"
    r"mixed|inconclusive|established|determined)\b"
)
# A verdict may itself be negated ("were not inconclusive", "did not remain
# mixed") or superseded by a later decisive win. "Neither method won", "None of
# the runs won" and "Ни один подход не победил" negate the win they contain.
# A numeric token counts as one word, so "was not 40% faster than B" is negated.
# "not only faster than B but also more accurate" amplifies the same result, so a
# negator followed immediately by "only"/"just"/"merely" does not negate anything.
_NEGATED_VERDICT = re.compile(
    r"(?i)(?:\b(?:not|never|no|no\s+longer|none|neither|nor|nobody|nothing|"
    r"не|ни|никто|ничего|жодн\w*)\b|n't)(?!\s*(?:only|just|merely)\b)"
    r"\s*(?:(?:\w+\S*)\s+){0,4}$"
)
# "The results were not inconclusive" negates the adjective inside the matched
# phrase, so a prefix-only check would still report a verdict the source rejects.
_NEGATED_INCONCLUSIVE = re.compile(
    r"(?i)\b(?:not|never|no\s+longer)\s+(?!only\b)(?:\w+\s+){0,3}"
    r"(?:inconclusive|mixed|undecided|unresolved|unsettled|open)\b"
    r"|n't\s+(?:\w+\s+){0,3}(?:inconclusive|mixed|undecided|unresolved)\b"
    r"|\bне\s+(?:\w+\s+){0,3}"
    r"(?:неопределенн|смешанн|противоречив|нерешенн|неразрешенн)\w*\b"
    # "Оба метода не показали равных результатов" states a difference, not an
    # equality verdict.
    r"|\bне\s+(?:\w+\s+){0,3}равн\w*\s+результат\w*\b"
)
# A directional measurement compares with an alternative, not with an expectation
# ("faster than expected"), a time period ("over the last quarter"), or the article
# left over when the optional "the" before the target is skipped ("than the
# previous version"). The leading guard rejects an article plus such a word, while
# a bare target keeps matching ("faster than A").
_ARTICLE = r"(?:the|a|an)"
_NON_ALTERNATIVE_TARGETS = (
    r"expected|anticipated|predicted|forecast|usual|before|previously|ever|"
    r"planned|budgeted|needed|required|previous|prior|last|past|earlier|same|time|"
    r"the|an|its|their|our|his|her|this|that|these|those"
)
_ALTERNATIVE_TARGET = (
    rf"(?!{_ARTICLE}\s+(?:{_NON_ALTERNATIVE_TARGETS})\b)"
    rf"(?:{_ARTICLE}\s+)?(?!{_NON_ALTERNATIVE_TARGETS}\b)\w+\b"
)
# A confident adverb does not turn a non-outcome into a comparison result.
_NON_OUTCOME_OUTPERFORM = (
    r"(?!\s+(?:expectations?|estimates?|forecasts?|projections?|consensus|"
    r"market|markets|targets?|peers?|industry|the\s+market|benchmarks?\s+of)\b)"
)
_DECISIVE_WIN = re.compile(
    r"(?i)(?:"
    # A bare "won" is the ordinary way a final round settles a comparison, but
    # "won praise", "won an award" and "won the contract" are not outcomes.
    r"(?<!['’])\bwon\b(?!['’]t)"
    rf"{_NON_OUTCOME_WIN}"
    rf"|\b(?:clearly|decisively|overall|comfortably|consistently)\s+won\b"
    rf"{_NON_OUTCOME_WIN}"
    r"|\b(?:was|is|were|are|proved|proves|emerged)\s+(?:as\s+)?(?:the\s+|a\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\b(?:became|becomes)\s+(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\bwinner\s+(?:was|is|became)\b"
    # "The winner was never announced" is an explicit absence, not a win.
    r"(?!\s+(?:never|not|unclear|unknown|undetermined|undecided|unresolved|"
    r"missing|absent)\b)"
    r"|\b(?:declared|established|determined|produced|identified|selected|named|"
    r"announced|found|measured|revealed)\s+(?:a\s+|the\s+)?(?:\w+\s+){0,2}winner\b"
    r"|\b(?:declared|named|announced)\s+(?:\w+\s+){1,3}?as\s+(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\ba\s+(?:clear\s+|single\s+|overall\s+|decisive\s+|outright\s+)?winner\s+"
    r"(?:emerged|was\s+(?:established|determined|selected))\b"
    r"|\b(?:final|overall|aggregate)\s+results?\s+"
    r"(?:favou?red|showed|confirmed)\s+(?:that\s+)?(?:\w+\s+){0,4}?"
    r"(?:ahead|winner|better|superior|best|first|outperform\w*)\b"
    r"|\b(?:comparison|question)\s+(?:was\s+)?resolved\s+in\s+favou?r\s+of\b"
    r"|\boutperform(?:ed|s)?\b"
    rf"{_NON_OUTCOME_OUTPERFORM}"
    r"|\bcame\s+out\s+ahead\b"
    r"|\b(?:clearly|strongly|consistently)\s+favou?red\b"
    r"|\bfavou?red\s+(?:the\s+)?(?:method|approach|strategy|option|model|version|"
    r"variant|system|tool|design|configuration|baseline|[a-z]\d*)\b"
    # "beat expectations" and "beat the record" are not comparison outcomes.
    r"|\bbeat(?:s|en)?\b(?!\s+(?:\w+\s+){0,2}(?:expectations?|estimates?|"
    r"forecasts?|projections?|consensus|records?|odds|targets?)\b)"
    r"|\b(?:clearly|decisively|comfortably|consistently)\s+superior\b"
    r"|\b(?:was|is|were|are|proved|proven|performed)\s+"
    r"(?:clearly\s+|consistently\s+|significantly\s+|slightly\s+)?superior\b"
    r"|\bclearly\s+(?:better|superior)\b"
    # A later, better-powered result settles what an earlier round left open.
    r"|\b(?:larger|bigger|full|final|follow-up|subsequent|later|second|confirmatory)\s+"
    r"(?:\w+\s+){0,2}?(?:trial|study|experiment|test|run|round|replication|"
    r"evaluation|analysis)\b[^.;]{0,50}?"
    r"\b(?:found|showed|confirmed|demonstrated|revealed)\b[^.;]{0,40}?"
    r"\b(?:superior|better|best|first|ahead|higher)\b"
    # "found a better way to run the test" is a recommendation, not a comparison
    # result; the better/superior claim needs a comparison predicate.
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\s+(?:that\s+)?"
    r"(?:\w+\s+){1,4}?\b(?:was|were|performed|fared|ranked|scored|did)\s+"
    r"(?:clearly\s+|significantly\s+)?(?:superior|better|best)\b"
    # "the test was better designed" evaluates design quality, not a comparison.
    r"(?!\s+(?:\w+ed|known|suited|understood|documented|maintained|written|"
    r"structured|organized|explained|described|named|equipped|prepared|"
    r"positioned)\b)"
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\s+(?:that\s+)?"
    r"(?:\w+\s+){1,3}?\b(?:superior|outperformed?)\b"
    r"|\bperformed\s+(?:better|best)\b"
    r"|\brank(?:ed|s)?\s+(?:\w+\s+){0,3}?(?:first|higher|above|ahead)\b"
    r"|\bplaced\s+(?:\w+\s+){0,3}?first\b"
    r"|\bcame\s+(?:in\s+)?first\b|\bfinished\s+(?:in\s+)?first\b"
    r"|\bput\s+(?:\w+\s+){0,3}?ahead\b"
    # A confirmed superlative ("the final benchmark found A was the fastest") is
    # a final standing, not merely an adjective. The frame requirement below keeps
    # "the fastest way to…" from becoming a comparison result.
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\b[^.;]{0,40}?"
    r"\b(?:was|were|is|are)\s+(?:the\s+)?"
    r"(?:fastest|slowest|best|most\s+(?:accurate|efficient|reliable|effective|"
    r"stable|scalable|secure))\b"
    r"|\b(?:was|were|is|are)\s+(?:the\s+)?"
    r"(?:fastest|slowest|best|most\s+(?:accurate|efficient|reliable|effective|"
    r"stable|scalable|secure))\b"
    r"|\b(?:scored|received|got|gave|earned)\b[^.;]{0,25}?\bhigher\s+score\b"
    r"|\bwas\s+(?:clearly\s+|significantly\s+|consistently\s+)?better\s+than\b"
    r"(?!\s+(?:expected|anticipated|predicted|forecast|usual|before|previously|"
    r"ever|planned|budgeted)\b)"
    # A measured directional comparison ("A was 40% faster than B", "A used 40%
    # less memory than B", "A cut latency by 40% vs B") reports a result the same
    # way "won" does. The frame requirement below keeps a bare observation beside
    # an open verdict from settling it, and "faster than expected" is not a
    # head-to-head result.
    r"|(?P<directional>"
    r"\b(?:was|were|is|are|ran|runs?|perform(?:ed|s)?|scored|finished|delivered|"
    r"achieved)\b(?:\s+(?:about|roughly|nearly|almost|over|only))?"
    r"[^.;]{0,15}?(?:\d+(?:\.\d+)?\s*(?:%|percent|x|times)\s+)?"
    r"(?:faster|slower|(?:more|less)\s+(?:accurate|precise|efficient|reliable|"
    r"effective|stable|scalable|secure|robust|consistent|responsive))"
    r"\s+than\s+"
    rf"{_ALTERNATIVE_TARGET}"
    r"|\b(?:used?|uses|required?|requires|consumed?|consumes|took|takes|spent|"
    r"needed)\b[^.;]{0,20}?(?:\d+(?:\.\d+)?\s*(?:%|percent)\s+)?"
    r"(?:less|more)\s+(?:memory|cpu|tokens?|time|energy|cost|latency|storage|"
    r"requests?|compute)\b[^.;]{0,10}?\bthan\s+"
    rf"{_ALTERNATIVE_TARGET}"
    r"|\b(?:has|have|had|showed|shows|delivered|achieved|scored|reported)\b"
    r"[^.;]{0,15}?(?:higher|lower)\s+(?:accuracy|precision|recall|throughput|score|"
    r"stability|latency|cost|memory|runtime|quality)\b"
    # "a higher accuracy target than the baseline" is a goal, not a measurement.
    r"(?!\s+(?:targets?|goals?|requirements?|thresholds?|bars?|standards?|"
    r"expectations?|budgets?)\b)[^.;]{0,10}?\bthan\s+"
    rf"{_ALTERNATIVE_TARGET}"
    r"|\b(?:reduced|cut|lowered|decreased|raised|increased|improved)\b"
    r"[^.;]{0,25}?\bby\s+\d+(?:\.\d+)?\s*(?:%|percent|x|times|ms|s|seconds|"
    r"minutes|hours)(?!\w)[^.;]{0,25}?"
    r"\b(?:vs\.?|versus|compared\s+(?:with|to)|than|over|against)\s+"
    rf"{_ALTERNATIVE_TARGET}"
    r")"
    r"|\bпобедил\w*\b|\bобогнал\w*\b|\bвыиграл\w*\b"
    r"|\bпереміг\w*\b|\bвиграв\w*\b|\bпереважив\w*\b"
    r"|\bпобед\w*\s+достал\w*ся\b|\bпризнан\w*\s+победител\w*\b"
    r"|\bоказал\w*ся\s+лучше\b|\bоднозначно\s+лучше\b"
    r"|\bпоказал\w*\s+лучш\w*\s+результат\w*\b|\bзанял\w*\s+первое\s+место\b"
    r"|\bпоказал\w*\s+вищ\w*\s+результат\w*\b"
    r")"
)
_SCOPE_OBJECTS = (
    r"(?:samples?|cases?|tasks?|workloads?|inputs?|queries|datasets?|benchmarks?|"
    r"rounds?|runs?|tests?|trials?|setups?|scenarios?|conditions?)"
)
_SCOPE_QUALIFIERS = (
    r"(?:small|large|larger|bigger|complex|simple|short|long|easy|hard|specific|"
    r"particular|narrow|single|subset|one|first|second|initial|early|later|final)"
)
# The same scope without a preposition ("won the small sample") does not take the
# round qualifier: "later rounds clearly favoured A" names the round that settled
# the comparison, not a subset of it.
_BARE_SCOPE_QUALIFIERS = (
    r"(?:small|large|larger|bigger|complex|simple|short|long|easy|hard|specific|"
    r"particular|narrow|single|subset|one|first|second|initial|early|final)"
)
# A "final/later round" names the round that produced the result, so it is a frame
# rather than a limit; these qualifiers name a genuine subset of the comparison.
_LIMITING_QUALIFIERS = (
    r"(?:small|complex|simple|short|long|easy|hard|specific|particular|narrow|"
    r"single|subset|one|first|second|initial|early)"
)
_SCOPE_METRICS = (
    r"\b(?:on|in|for)\s+(?:the\s+)?(?:accuracy|speed|latency|throughput|stability|"
    r"memory|cost|recall|precision|f1|quality|security|coverage|size|energy|"
    r"runtime|time|price|reliability)\b"
)
# "under peak load" is a named operating condition, not the overall standing.
_SCOPE_CONDITIONS = (
    r"\b(?:on|in|for|under|with|at)\s+(?:the\s+)?"
    r"(?:peak|high|low|heavy|light|nominal|realistic)\s+(?:loads?|traffic|conditions?)\b"
)
_SCOPE_LOSSES = (
    r"\b(?:lost|loses|lagged|trailed|fell\s+behind|was\s+slower|did\s+worse|"
    r"performed\s+worse|underperformed)\b"
)
# "There is not enough evidence to determine the cause" states ordinary uncertainty
# about a fact; the same words are a comparison verdict only when the purpose of the
# missing data is the outcome of a comparison. The purpose, not a comparison noun in
# a neighbouring clause, decides it.
_INSUFFICIENCY_VERDICT = re.compile(
    r"(?i)(?:"
    r"\b(?:too\s+(?:few|small|limited|little)|not\s+enough)\b[^.;]{0,60}?"
    r"\bto\s+(?:pick|choose|decide|determine|declare|select|tell|distinguish)\b"
    r"|\b(?:данных|доказательств|сведений|даних|доказів)\s+"
    r"(?:(?:пока|все\s+еще|досі)\s+)?(?:недостаточно|недостатньо|мало)\b"
    r"|\b(?:недостаточно|недостатньо|мало)\s+"
    r"(?:данных|доказательств|сведений|даних|доказів)\b"
    r")"
)
# The complement scopes the missing data: "to determine <the cause>" and
# "чтобы определить <причину>" both name what the uncertainty is about. The English
# mention form already consumes its verb, the Russian one does not, so the leading
# separator, "to/чтобы/щоб" and an optional purpose verb are stripped from the tail.
_INSUFFICIENCY_PURPOSE_PREFIX = re.compile(
    r"(?i)^[\s,;:—–-]*(?:(?:to|чтобы|щоб)\s+)?"
    r"(?:(?:pick|choose|decide|determine|declare|select|tell|know|say|understand|"
    r"установить|определить|выяснить|понять|решить|выбрать|сказать|визначити|"
    r"з'ясувати|обрати|вирішити)\b\s*)?"
)
_COMPARISON_SIDE_START = (
    rf"(?:(?:the|these|those|two|both|двух|двумя|обоих|обоими|этих|этими|"
    rf"двох|двома|обох|обома|цих|цими)\s+)*{_COMPARISON_SIDES}\b"
    rf"(?:\s+(?:[A-Za-zА-Яа-я]|\d{{1,2}})\b)?"
)
# Only a purpose that selects the comparison outcome — a winner, the better side,
# "which one" — turns insufficiency into a no-winner verdict. "Determine the cause",
# "whether to migrate", "the impact on cost" and "their safety" stay ordinary
# uncertainty about a fact even when a comparison word appears in the sentence.
_INSUFFICIENCY_COMPARISON_OUTCOME = re.compile(
    r"(?i)(?:"
    r"\b(?:a|the|one)\s+(?:clear\s+|single\s+|overall\s+|definitive\s+|true\s+)?winner\b"
    r"|\b(?:the\s+)?(?:clear\s+|overall\s+|definitive\s+)?winning\s+"
    r"(?:approach|method|model|option|variant|configuration|tool|system)\b"
    r"|\b(?:the\s+)?(?:best|better|faster|stronger|superior|cheaper)\s+"
    r"(?:one|approach|method|model|option|variant|configuration|tool|system)\b"
    r"|\bwhich\s+(?:one|of\s+(?:them|the\s+two|these))\b"
    r"|\bwhich\s+(?:approach|method|model|option|variant|tool|system|algorithm|"
    r"configuration|technique|baseline)\b[^.;]{0,40}?"
    r"\b(?:is|was|performs?|performed|works?|worked|wins?|won|comes?|came|does|did|"
    r"ran|runs|should)\b"
    r"|\bwhich\s+(?:is|was)\s+(?:better|faster|more\s+\w+|stronger|cheaper)\b"
    r"|\b(?:whether|if)\b[^.;]{0,40}?\b(?:is|was|are|were|performs?|performed|"
    r"does|did|works?|worked)\s+(?:better|faster|worse|cheaper|stronger|superior|"
    r"more\s+(?:accurate|efficient|stable|reliable|scalable|robust|precise|effective)|"
    r"less\s+(?:accurate|efficient|stable|reliable|scalable|robust|precise|effective))\b"
    r"|\bwho\s+(?:won|wins|is\s+better|came\s+out\s+ahead)\b"
    r"|\b(?:tell|distinguish)\s+(?:them|the\s+two|these|the\s+"
    r"(?:methods|approaches|models|options|variants|systems|tools))\s+apart\b"
    rf"|\bdistinguish\s+between\s+{_COMPARISON_SIDE_START}"
    r"|\bthe\s+two\s+(?:methods|approaches|models|options|variants|systems|tools|"
    r"algorithms|agents|prompts)\s+apart\b"
    # An unresolved choice between two sides is the comparison outcome even when
    # the source never says "winner": "choose between the two methods". Only a
    # comparison side makes the choice a comparison: a decision between actions
    # ("between migrating and staying") and a relationship ("between price and
    # demand") stay ordinary uncertainty about a fact.
    rf"|\bbetween\s+{_COMPARISON_SIDE_START}"
    rf"|\bмежду\s+{_COMPARISON_SIDE_START}"
    rf"|\bміж\s+{_COMPARISON_SIDE_START}"
    r"|\bодин\s+из\s+(?:двух|них)\b"
    r"|\bодин\s+з\s+(?:двох|них)\b"
    r"|победител\w*"
    r"|перемож\w*"
    r"|\bкращ\w+\s+"
    r"(?:метод|підхід|варіант|модель|інструмент|алгоритм|систем|конфігураці)\w*"
    r"|\bкто\s+лучше\b"
    r"|\b(?:что|какой|какая|какое|какие|который|которая|которое)\s+"
    r"(?:из\s+(?:них|двух)\s+)?лучше\b"
    r"|\b(?:какой|который)\s+(?:из\s+(?:них|двух)\s+)?"
    r"(?:метод|подход|вариант|модель|инструмент|алгоритм|систем)\w*\s+лучше\b"
    r"|\bлучш\w+\s+"
    r"(?:метод|подход|вариант|модель|инструмент|алгоритм|систем|конфигураци)\w*"
    r")"
)
# A comparison noun inside a possessive or a non-outcome "of" phrase describes an
# attribute of an outcome the source already established ("the winner's margin",
# "the winner of the tender"), so it does not make the missing data a no-winner
# verdict about the comparison itself. Russian and Ukrainian put the non-outcome
# object in the genitive right after the noun ("победителя тендера").
_ATTRIBUTIVE_OUTCOME_TAIL = re.compile(
    rf"(?i)\s*(?:['’]s\b"
    rf"|\bof\s+(?:the\s+|a\s+|an\s+)?(?:[\w-]+\s+)?(?:{_NON_OUTCOME_WIN_OBJECTS})\b"
    rf"|(?:{_NON_OUTCOME_WIN_OBJECTS_CYRILLIC})\b)"
)
# A causal, explanatory or measurement head scopes the uncertainty to an attribute
# of a known outcome ("why the winning approach worked", "the cause of the winning
# approach's advantage", "почему лучший подход работает в продакшене"); it is not
# a purpose that selects the comparison outcome.
_ATTRIBUTIVE_PURPOSE_HEAD = re.compile(
    r"(?i)\b(?:cause|reason|explanation|margin|advantage|impact|effect|extent|"
    r"degree|size|source|mechanism|driver|factor)s?\b"
    # A relationship, dependency or compatibility between two systems is a fact
    # about how they relate, not which one wins.
    r"|\b(?:relationships?|relations?|interactions?|dependenc(?:y|ies)|"
    r"correlations?|connections?|compatibility|associations?|linkages?|links?)\b"
    r"|\bwhy\b|\bhow\b(?!\s+(?:much|many|far|well))"
    r"|\b(?:причин\w*|почему|зачем|объяснени\w*|механизм\w*|размер\w*|величин\w*|"
    r"влияни\w*|эффект\w*|степен\w*|источник\w*|фактор\w*|отношени\w*|"
    r"взаимосвяз\w*|взаимодействи\w*|зависимост\w*|корреляц\w*|связ\w*|"
    r"совместимост\w*|интеграц\w*|согласованност\w*)"
    r"|\b(?:чому|пояснен\w*|вплив\w*|ефект\w*|ступінь\w*|джерел\w*|фактор\w*|"
    r"відношень|відносин|відношенн\w*|взаємоді\w*|залежн\w*|кореляц\w*|"
    r"зв'яз\w*|сумісност\w*|інтеграц\w*)"
)
# A named side is an alternative noun with a short label ("method A", "вариант 1").
# The label run covers coordinated names ("methods A and B", "варианты 1/2");
# "метод лечения" is the topic of the uncertainty, not one of two compared sides.
_NAMED_ALTERNATIVE = re.compile(
    r"(?i)\b(?:methods?|approaches?|models?|options?|variants?|configurations?|"
    r"baselines?|agents?|prompts?|systems?|tools?|algorithms?|"
    r"метод\w*|подход\w*|вариант\w*|модел\w*|алгоритм\w*|систем\w*|"
    r"инструмент\w*|конфигурац\w*|варіант\w*|підхід\w*)\b"
)
_ALTERNATIVE_LABEL_RUN = re.compile(
    r"(?i)^\s*(?:[a-zа-я]|\d{1,2})(?=\b)"
    r"(?:\s*(?:,|/|&|and|и|та)\s*(?:[a-zа-я]|\d{1,2})(?=\b))*"
)
_ALTERNATIVE_LABEL = re.compile(r"\b(?:[a-zа-я]|\d{1,2})\b", re.IGNORECASE)


def _scope_pattern(qualified: str, bare: str) -> str:
    """Regex text for a win that only covers part of a comparison."""
    return (
        r"(?i)\b(?:on|in|for|under|with|at)\s+(?:the\s+|a\s+|an\s+)?"
        rf"{qualified}\s+{_SCOPE_OBJECTS}\b"
        # A win on one measured dimension is partial when the verdict is still open
        # ("A won on speed, B won on stability"); an overall marker overrides it.
        f"|{_SCOPE_METRICS}|{_SCOPE_CONDITIONS}"
        r"|\bfor\s+(?:small|large|complex|simple|short|long|easy|hard)\s+"
        r"(?:inputs?|tasks?|queries|cases?|samples?)\b"
        # "the tool was better at scale" is a scoped observation, not an overall win.
        r"|\bat\s+scale\b|\bin\s+production\b"
        # "won the small sample" names the sub-scope without a preposition.
        rf"|\b(?:the\s+|a\s+|an\s+)?{bare}\s+{_SCOPE_OBJECTS}\b"
    )


# A win in a named sub-scope ("on small samples", "under peak load") is partial
# and does not settle the whole comparison unless the sentence also speaks overall.
# A mention of a loss beside the win ("A won but B lagged on writes") is also a
# partial signal, but not a limit the final round cannot override.
_PARTIAL_WIN_SCOPE = re.compile(
    f"{_scope_pattern(_SCOPE_QUALIFIERS, _BARE_SCOPE_QUALIFIERS)}|{_SCOPE_LOSSES}"
)
# A result framed by its final round still does not turn a subset of the comparison
# ("on the small sample", "on accuracy", "under peak load") into the overall
# standing.
_LIMITING_WIN_SCOPE = re.compile(_scope_pattern(_LIMITING_QUALIFIERS, _LIMITING_QUALIFIERS))
# A ranking verb is only a comparison result when its sentence names a comparison
# or experiment. "Costs ranked first among user concerns" is not a winner.
_COMPARISON_CONTEXT = re.compile(
    r"(?i)\b(?:comparisons?|compared|versus|vs\.?|benchmarks?|trials?|studies|study|"
    r"experiments?|tests?|evaluations?|analys[ie]s|rounds?|runs?|replications?|"
    r"follow-ups?|methods?|approaches?|models?|systems?|tools?|strategies|"
    r"strategy|algorithms?|techniques?|baselines?|variants?|options?|"
    r"configurations?|agents?|prompts?|samples?|datasets?|workloads?|"
    r"results?|findings?|outcomes?|verdicts?|metrics?)\b"
)
# These predicates can describe a race, a quiz, a priority list, or a ranking of
# concerns. They need the comparison context above before they settle a verdict.
_AMBIGUOUS_WIN = re.compile(r"(?i)^(?:rank|ranked|ranks|placed|came|finished|put)\b")
_OVERALL_MARKER = re.compile(
    # "The overall comparison was inconclusive" names the comparison itself, not an
    # overall result, so the noun after the qualifier decides whether it frames one.
    r"(?i)\b(?:overall|final|aggregate|decisive|conclusive)\s+"
    r"(?:results?|outcome|winner|verdict|conclusions?|score|picture|pattern|"
    r"round|analysis|comparison\s+results?|trials?|runs?|tests?|experiments?|"
    r"studies|study|benchmarks?|replications?|evaluations?)\b"
    r"|\bconfirmed\b|\bacross\s+(?:all|both)\b|\bin\s+general\b|"
    r"\bon\s+average\b|\bacross\s+the\s+board\b|"
    r"\b(?:on|across)\s+(?:all|every)\s+(?:metrics?|tasks?|workloads?|benchmarks?|cases?)\b"
)
# A standalone sentence-initial discourse marker frames the sentence as the end
# result: "Overall, A won", "In the end, A won", "В итоге победил метод A". The
# marker must end its own phrase, so "Overall costs rose" stays a cost statement
# and "In the end, A won on the small sample" stays partial (its own claim has no
# overall frame). Without this frame a bare final win was not tied to the earlier
# open comparison, and the repair appended a false no-winner verdict (COR-26-01).
_SENTENCE_RESULT_FRAME = re.compile(
    r"(?i)^\s*(?:(?:and|but|so|then)\s+)?(?:"
    r"(?:overall|in\s+the\s+end|in\s+the\s+final\s+analysis|ultimately|all\s+in\s+all|"
    r"at\s+the\s+end\s+of\s+the\s+day|when\s+all\s+is\s+said\s+and\s+done)\s*,"
    r"|(?:в\s+итоге|в\s+конце\s+концов|в\s+целом|в\s+конечном\s+счете|"
    r"у\s+підсумку|зрештою|загалом|кінець\s+кінцем|в\s+результаті)\b)"
)
# A later win settles an earlier open verdict only when it is framed as a result
# of that comparison: a final/overall/larger/follow-up experiment. "Costs ranked
# first among user concerns", "our team won the office quiz" and "the tool was
# better at scale" are unrelated or scoped wins, not a resolution.
_LATER_RESULT_FRAME = re.compile(
    r"(?i)\b(?:overall|final|aggregate|decisive|conclusive)\s+"
    r"(?:\w+\s+){0,2}?(?:results?|outcome|winner|verdict|conclusions?|score|"
    r"picture|pattern|round|analysis|comparisons?\s+results?|trials?|runs?|"
    r"tests?|experiments?|studies|study|benchmarks?|replications?|evaluations?)\b"
    r"|\b(?:larger|bigger|full|follow-up|subsequent|later|second|confirmatory|"
    r"current|latest|extended|replication)\s+(?:\w+\s+){0,2}?"
    r"(?:results?|benchmarks?|trials?|studies|study|experiments?|tests?|runs?|"
    r"rounds?|replications?|evaluations?|analys[ie]s|comparisons?)\b"
    r"|\b(?:across\s+(?:all|both)|in\s+general|on\s+average|across\s+the\s+board)\b"
    r"|\b(?:after|following|across|over)\s+(?:\w+\s+){0,4}?"
    r"(?:runs?|rounds?|trials?|tests?|experiments?|replications?|evaluations?)\b"
    r"|\bfollow-up\b|\breplication\b"
    # A Russian or Ukrainian final test/benchmark frames the result the same way.
    r"|\b(?:итогов\w*|финальн\w*|підсумков\w*|фінальн\w*)\s+"
    r"(?:\w+\s+){0,2}?(?:тест\w*|бенчмарк\w*|результат\w*|раунд\w*|"
    r"испытан\w*|эксперимент\w*|замер\w*|прогон\w*|сравнени\w*|оценк\w*|"
    r"дослід\w*|випробуван\w*|порівнян\w*)"
    r"|\b(?:по\s+итогам|за\s+підсумками)\b"
)
# Some win forms are an explicit head-to-head verdict, not a bare "won" that a
# quiz or a priority list can also produce.
_UNAMBIGUOUS_WIN = re.compile(
    r"(?i)\b(?:clearly|decisively|comfortably|consistently)\s+"
    r"(?:(?:superior|better|favou?red)\b"
    rf"|won\b{_NON_OUTCOME_WIN})"
    r"|\bbeat(?:s|en)?\b(?!\s+(?:\w+\s+){0,2}(?:expectations?|estimates?|"
    r"forecasts?|projections?|consensus|records?|odds|targets?)\b)"
    r"|\boutperform(?:ed|s)?\b"
    rf"{_NON_OUTCOME_OUTPERFORM}"
    r"|\b(?:became|becomes|announced|declared|named|selected|established|"
    r"determined|produced|identified)\b[^.;]{0,30}?\b(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\bwinner\s+(?:was|is|became)\b"
    r"|\bcomparison\b[^.;]{0,30}?\b(?:was\s+)?resolved\s+in\s+favou?r\s+of\b"
    r"|\bwas\s+(?:clearly\s+|significantly\s+|consistently\s+)?superior\b"
    r"|\b(?:won|wins|better|superior|best|first|ahead)\s+(?:overall|on\s+average)\b"
    # "Метод A явно победил" is an explicit head-to-head verdict, not a bare win
    # that a tender or a quiz can also produce.
    r"|\b(?:явно|однозначно|уверенно|очевидно)\s+"
    r"(?:победил\w*|переміг\w*|виграв\w*)\b"
)
# A directional comparison that is itself framed as the overall standing settles
# the comparison ("A was faster than B overall", "A cut latency by 40% vs B
# overall"). Only a directional match may take this shortcut: a quiz or a priority
# list must still need an explicit winner declaration or result frame.
_OVERALL_RESULT = re.compile(
    r"(?i)\b(?:overall|on\s+average|in\s+general|across\s+the\s+board|in\s+aggregate)\b"
)
_CONTRAST_COORDINATOR = re.compile(
    r"(?i)\b(?:but|while|whereas|yet|although|though|and|однако|но|а|и|і)\b"
)
# An advantage in a second dimension may be a quality, a resource, or a plain
# comparative adjective ("cheaper", "smaller"); "not only more accurate but also
# more stable" repeats the target's edge, so one match is enough.
_SPLIT_EDGE = (
    r"more\s+(?:accurate|precise|efficient|reliable|effective|stable|scalable|"
    r"secure|robust|consistent|responsive|memory|cpu|time|energy|cost|latency|"
    r"storage|tokens?|compute|requests?)|less\s+(?:memory|cpu|time|energy|cost|"
    r"latency|storage|tokens?|compute|requests?)|fewer\s+(?:errors?|failures?|"
    r"retries?|requests?|tokens?|bugs?|incidents?)|higher\s+(?:accuracy|precision|"
    r"recall|throughput|score|stability|quality)|better|superior|faster|cheaper|"
    r"smaller|lighter|quicker|simpler|cleaner|shorter|leaner|tighter|safer|easier"
)
# A contrast that hands the comparison target its own advantage ("A was 40% faster
# than B, but B was more accurate") reports a split result, not a winner. The
# opponent capture is compared with the match's comparison target, so a caveat
# that still favours the winner ("but A was also more accurate") does not cancel it.
_SPLIT_OPPONENT_CLAIM = re.compile(
    r"(?i)\b(?:but|however|while|whereas|yet|although|though|однако|но|а)\b\s+"
    r"(?:the\s+|a\s+|an\s+)?(?P<opponent>(?:[\w-]+\s+)?[\w-]+)"
    r"[^.;]{0,40}?\b(?:was|were|is|are|had|has|used|uses|required?|requires|took|"
    r"takes|spent|cut|reduced|lowered|raised|increased|improved|scored|performed|"
    r"delivered|achieved|produced?|produces|generated?|generates|showed|shows|"
    rf"reported?|recorded?|ran|did)\b[^.;]{{0,15}}?(?:{_SPLIT_EDGE})\b"
)
# "and" may join two embedded questions under one verb, and it may join a
# measurement with its qualifier, so only a strong contrast starts a new claim.
_STRONG_CONTRAST = re.compile(
    r"(?i)\b(?:but|however|while|whereas|yet|although|though|однако|но|а)\b"
)
# A decisive win in another experiment or source does not resolve the comparison
# that the inconclusive verdict was about.
_SEPARATE_COMPARISON = re.compile(
    r"(?i)\b(?:separate|different|another|independent|unrelated|distinct)\s+"
    r"(?:experiments?|studies|study|trials?|benchmarks?|comparisons?|tests?|"
    r"datasets?|tasks?|workloads?|sources?|videos?|articles?)\b"
    r"|\b(?:in|on)\s+(?:a\s+)?(?:separate|different|another)\s+"
    r"(?:experiment|study|trial|benchmark|test|dataset|task|workload)\b"
)
_SOURCE_MARKER = re.compile(r"(?im)^\s*(?:source|источник)\s*(\d+)\s*[:.]")
# A verdict attributed to prior work is background, not this source's conclusion.
# It must not append uncertainty to a title that reports the current result.
_PRIOR_WORK = re.compile(
    r"(?i)\b(?:prior|earlier|previous|past|existing|previously\s+published)\s+"
    r"(?:studies|study|research|work|results|findings|experiments|trials|"
    r"benchmarks|comparisons|analyses|reports|authors|teams|rounds|runs)\b"
    r"|\b(?:researchers|others|authors)\s+(?:previously|earlier|had\s+previously)\b"
    r"|\b(?:более\s+ранн\w*|предыдущ\w*|прежн\w*|прошл\w*|ранн\w*|попередн\w*|"
    r"раніше)\s+(?:исследован\w*|работ\w*|эксперимент\w*|испытан\w*|обзор\w*|"
    r"досліджень|робіт|експеримент\w*|даних)\b"
)
# "Mixed results from earlier studies are reviewed" attributes the verdict to
# prior work that follows the phrase; the current source reports no result of its own.
_PRIOR_WORK_AFTER = re.compile(
    r"(?i)^[^.;]{0,40}?(?:"
    r"\b(?:from|in|by|of|across)\s+(?:\w+\s+){0,3}?"
    r"(?:prior|earlier|previous|past|existing)\s+"
    r"(?:studies|study|research|work|results|findings|experiments|trials|"
    r"benchmarks|comparisons|analyses|reports|authors|teams|rounds|runs)\b"
    r"|\b(?:более\s+ранн\w*|предыдущ\w*|прежн\w*|прошл\w*|попередн\w*|раніше)\s+"
    r"(?:исследован\w*|работ\w*|эксперимент\w*|испытан\w*|обзор\w*|досліджень|робіт)"
    r")"
)
# Explanatory prose about a phenomenon ("the article explains why mixed results
# are common") describes the topic; it is not this material's own verdict.
_META_WHY_BEFORE = re.compile(
    r"(?i)\b(?:explain\w*|discuss\w*|describ\w*|review\w*|examin\w*|analyz\w*|"
    r"объясня\w*|описыва\w*|рассматрива\w*|обсужда\w*|разбира\w*|поясню\w*|"
    r"розповіда\w*|опису\w*|поясню\w*|"
    r"explica\w*|describ\w*|analiz\w*|discut\w*)\b[^.;]{0,50}?"
    r"\b(?:why|how|почему|зачем|как|чому|як|что|por\s+qu[eé])\b"
)
# "The article explains how comparisons work and why mixed results are common"
# coordinates two embedded questions under one explanatory verb. The second
# segment starts with a question word, so the meta verb still governs it; a plain
# "and" joining a main claim ("explains caching and mixed results were found")
# does not.
_META_COORDINATED = re.compile(
    r"(?i)\b(?:and|и)\s+(?:why|how|whether|почему|зачем|как|чому|як)\b"
    r"(?:(?!\b(?:and|but|however|но|и|а)\b)[^.;])*$"
)
_META_VERB = re.compile(
    r"(?i)\b(?:explain\w*|discuss\w*|describ\w*|review\w*|examin\w*|analyz\w*|"
    r"объясня\w*|описыва\w*|рассматрива\w*|обсужда\w*|разбира\w*|поясню\w*|"
    r"розповіда\w*|опису\w*|explica\w*|describ\w*|analiz\w*|discut\w*)\b"
)
_HYPOTHETICAL_WIN = re.compile(r"(?i)\b(?:whether|if|если|ли)\b")
_HEDGED_WIN = re.compile(
    r"(?i)\b(?:might|may|could|would|possibly|perhaps|potentially|likely|expected|"
    r"anticipated|projected|forecast|"
    r"предположительно|возможно|вероятно)\b"
)
_RETRACTED_CLAIM = re.compile(
    r"(?i)\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws)|correct(?:ed|s)?|"
    r"revok(?:ed|es)?|overturn(?:ed|s)?)\b[^.;]{0,60}?"
    r"\b(?:that|this|the|its|their|earlier)?\s*"
    r"(?:win|winner|result|finding|conclusion|verdict|claim)\b"
    r"|\b(?:that|this|the)\s+(?:win|result|finding|conclusion|verdict|claim)\b"
    r"[^.;]{0,60}?\b(?:was|were|is|are|has\s+been|had\s+been)\s+"
    r"(?:\w+\s+){0,2}?(?:retracted|withdrawn|corrected|revoked|overturned)\b"
)
_RETRACTED_VERDICT = re.compile(
    r"(?i)\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws)|correct(?:ed|s)?|"
    r"revok(?:ed|es)?)\b[^.;]{0,60}?"
    r"\b(?:inconclusive|undetermined|mixed|no[- ]winner|uncertain\w*|"
    r"неопределенн\w*|смешанн\w*)\b"
    r"|\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws))\b[^.;]{0,40}?"
    r"\b(?:that|this|the)\s+(?:conclusion|finding|verdict|result)\b"
    r"|\b(?:inconclusive|undetermined|mixed|no[- ]winner)\b[^.;]{0,60}?"
    r"\b(?:was|were|is|are|has\s+been|had\s+been)\s+"
    r"(?:retracted|withdrawn|corrected|revoked)\b"
)
# A period between digits is a decimal point ("20.5 percent", "2.5 ms"), not a
# sentence end, so a measured value stays inside one sentence and the label rule sees
# the whole number instead of a truncated "plan 20." ordinal.
_SENTENCE = re.compile(r"(?:(?:[^.!?\n])|(?:(?<=\d)\.(?=\d)))+(?:[.!?]+|$)")


def _fold(text: str) -> str:
    """Normalize ё for matching only; the original case is kept; nothing is rewritten.

    Every pattern except the label rule matches case-insensitively, so lowercasing the
    evidence would not find a word any better — but it would erase the one place where
    case carries meaning. A single letter after a lowercase plan-like baseline labels
    an alternative when it is uppercase ("plan A", "плана В по задержке") and is the
    English article or a Russian conjunction/preposition when it is lowercase ("plan a
    year ago", "плана и по цене"), so casefold made ordinary prose read as a plan label
    (QUAL-40-01); in case-normalized evidence the baseline's own case is the context,
    so "PLAN A YEAR AGO" reads the letter as prose (COR-41-01).
    """
    return text.translate(_FOLD)


_clause_before = clause_before


def _negated_at(text: str, index: int) -> bool:
    return _NEGATED_VERDICT.search(_clause_before(text, index)) is not None


def _label_number_is_a_label(sentence: str, end: int) -> bool:
    """Whether the integer ending at ``end`` is a designator, not a measured value."""
    rest = sentence[end:]
    if _LABEL_VALUE_SHAPE.match(rest) is not None:
        return False
    return _LABEL_QUANTITY_WORD.match(rest) is None


def _label_token_at(sentence: str, position: int, *, mixed_case: bool) -> bool:
    """Whether a designator starts at ``position``.

    A small ordinal or an explicit ``#2``/``№2`` label names an alternative wherever
    it stands; a measured value does not. A one-letter label names an alternative
    unless the letter is also an article, preposition, conjunction or particle and
    the writing is case-normalized: ``mixed_case`` is true only when the baseline
    before the letter is written lowercase, which is the one context where an
    uppercase letter labels a side ("plan A") and its lowercase homograph is prose
    ("plan a year ago"). Reading the letter's case alone made "PLAN A YEAR AGO" and
    "Plan A Year Ago" name a side and put a no-winner suffix on a title that states
    the winner (COR-41-01).
    """
    if _LABEL_EXPLICIT_NUMBER.match(sentence, position) is not None:
        return True
    number = _LABEL_NUMBER.match(sentence, position)
    if number is not None:
        return _label_number_is_a_label(sentence, number.end())
    letter = _LABEL_LETTER.match(sentence, position)
    if letter is None:
        return False
    token = letter.group()
    if token.casefold() not in _LABEL_SERVICE_WORDS:
        return True
    return mixed_case and token.isupper()


def _designator_names_a_side(sentence: str, baseline: re.Match[str]) -> bool:
    """Whether a plan-like baseline in ``sentence`` carries a designator.

    Only a plan-like baseline can name an alternative; every other baseline stays a
    graded reference whatever follows it ("the estimate 20 ms", "the projection 40"
    measure one outcome, not two sides). The label is read from the text after the
    match rather than by a lookahead inside the pattern, because the reading needs
    the case of the baseline word itself.
    """
    alternative = baseline.group("alternative")
    if alternative is None:
        return False
    separator = _LABEL_SEPARATOR.match(sentence, baseline.end())
    position = separator.end() if separator is not None else baseline.end()
    return _label_token_at(sentence, position, mixed_case=alternative.split()[-1].islower())


def _side_cue_after(sentence: str, start: int, end: int) -> bool:
    """Whether the clause compares sides; a graded expectation is not a side.

    The expectation baseline is read from the comparative's position in the whole
    sentence because the clause break sits between them in "…was better, than
    expected" and "…лучше, чем ожидалось". A comparative that grades an expectation
    says nothing about which side won, so a further cue in the same clause still
    counts; a plan-like baseline that carries a designator names a side instead.
    """
    while start < end:
        window = sentence[start:end]
        match = _SIDE_CUE_AFTER.search(window)
        if match is None:
            return False
        if match.group("graded") is None:
            return True
        start += match.end()
        baseline = _EXPECTATION_BASELINE.match(sentence, start)
        if baseline is None or _designator_names_a_side(sentence, baseline):
            return True
    return False


def _names_compared_sides(sentence: str) -> bool:
    """Whether the sentence compares named sides, not just mentions a side noun.

    "No winner was announced in the tender for the new monitoring system" names the
    event's object; the later "Acme won the tender" still settles the event, so the
    no-winner phrase must not become a comparison verdict (COR-25-01 residual). A
    sentence that actually compares sides ("the two systems differed", "comparison
    of the two methods") is a comparison verdict even when it also names an event.
    """
    for side in _COMPARISON_SIDE_NAMES.finditer(sentence):
        clause_start = 0
        for boundary in CLAIM_CLAUSE_BREAK.finditer(sentence[: side.start()]):
            clause_start = boundary.end()
        clause_end_match = CLAIM_CLAUSE_BREAK.search(sentence, side.end())
        clause_end = clause_end_match.start() if clause_end_match else len(sentence)
        if _side_cue_after(sentence, side.end(), clause_end):
            return True
        if _SIDE_CUE_BEFORE.search(sentence[clause_start : side.start()]):
            return True
    return False


def _event_scoped_verdict(
    text: str,
    spans: list[tuple[int, int]],
    starts: list[int],
    match: re.Match[str],
) -> bool:
    """Whether a no-winner phrase reports an event outcome, not a comparison.

    "No winner was announced in the election" and "в рамках тендера победителя
    нет" name an event, not compared sides. Treating them as a comparison verdict
    appended "no winner was established" to a title that the same source contradicts
    ("Smith won the election").
    """
    sentence_index = _sentence_at(spans, starts, match.start())
    if sentence_index is None:
        return False
    start, end = spans[sentence_index]
    sentence = text[start:end]
    if _names_compared_sides(sentence):
        return False
    return _EVENT_VERDICT_FRAME.search(sentence) is not None


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end()) for match in _SENTENCE.finditer(text) if match.group().strip()
    ]


def _sentence_at(spans: list[tuple[int, int]], starts: list[int], index: int) -> int | None:
    position = bisect_right(starts, index) - 1
    if position >= 0:
        start, end = spans[position]
        if start <= index < end:
            return position
    return None


def _source_markers(text: str) -> list[tuple[int, int]]:
    """Numbered source-block starts, built once per detection call."""
    return [(match.start(), int(match.group(1))) for match in _SOURCE_MARKER.finditer(text)]


def _source_at(markers: list[tuple[int, int]], index: int) -> int:
    """Which numbered source block a position belongs to; 0 means unlabelled."""
    position = bisect_right(markers, (index, sys.maxsize))
    return markers[position - 1][1] if position else 0


# Every decisive form contains one of these literals, so a sentence without them
# skips the large alternation entirely (the pre-check reads the sentence case-folded).
_DECISIVE_HINTS = (
    "won",
    "winner",
    "beat",
    "superior",
    "better",
    "best",
    "fastest",
    "slowest",
    "outperform",
    "ahead",
    "favor",
    "favour",
    "resolved",
    "first",
    "higher",
    "score",
    "result",
    "faster",
    "slower",
    "less",
    "more",
    "reduced",
    "raised",
    "cut ",
    "lower",
    "decreased",
    "increased",
    "improved",
    " compared",
    " vs",
    "побед",
    "обогнал",
    "выиграл",
    "признан",
    "лучше",
    "результат",
    "место",
    "переміг",
    "виграв",
)


def _claim_window(sentence: str, clause: str, match: re.Match[str]) -> str:
    """The single claim a win belongs to: its prefix clause, the win, its own tail.

    An "overall" in a neighbouring clause ("…, and overall costs rose") or a
    sentence-leading discourse marker ("Overall, the final benchmark found A won on
    the small sample") frames that other clause, not this win, so only the clause
    that contains the win can make it an overall result.
    """
    tail = re.split(r"[,;:.!?]", sentence[match.end() :], maxsplit=1)[0]
    return f"{clause} {match.group()} {tail}"


def _win_mention_rejected(sentence: str, clause: str, match: re.Match[str]) -> bool:
    """Shared rejection filters for a decisive-win mention.

    Used both for a usable result and for a subset win that leaves the comparison
    open, so a negated, hedged, hypothetical, ambiguous, or separate-experiment claim
    can never be read as either.
    """
    directional = match.group("directional") is not None
    # "ranked first", "finished first" and "put A ahead" also describe quizzes,
    # races and priority lists; only a comparison context makes them a result.
    if _AMBIGUOUS_WIN.search(match.group()) and not _COMPARISON_CONTEXT.search(sentence):
        return True
    # A negation can sit inside the matched comparison ("found that no method
    # was better"), so the check covers the match itself, not just its prefix.
    if _negated_at(sentence, match.start()):
        return True
    if _NEGATED_VERDICT.search(f"{clause} {match.group()} "):
        return True
    if _HYPOTHETICAL_WIN.search(clause) or _HEDGED_WIN.search(clause):
        return True
    # A hedge inside the measurement ("was likely 40% faster than B") makes it a
    # forecast, not a result; the clause check above cannot see this position.
    if directional and _HEDGED_WIN.search(match.group()):
        return True
    return _SEPARATE_COMPARISON.search(sentence) is not None


def _usable_wins_in_sentence(
    text: str,
    spans: list[tuple[int, int]],
    sentence_index: int,
    markers: list[tuple[int, int]],
) -> list[tuple[re.Match[str], int]]:
    """Decisive win mentions in one sentence, each with its numbered source.

    The cheap literal pre-check reads the sentence case-folded, because ``text`` keeps
    the source case for the label rule.
    """
    sentence_start, sentence_end = spans[sentence_index]
    sentence = text[sentence_start:sentence_end]
    if not any(hint in sentence.casefold() for hint in _DECISIVE_HINTS):
        return []
    usable: list[tuple[re.Match[str], int]] = []
    for match in _DECISIVE_WIN.finditer(sentence):
        local_start = match.start()
        clause = _clause_before(sentence, local_start)
        directional = match.group("directional") is not None
        if _win_mention_rejected(sentence, clause, match):
            continue
        # A win that only holds for a named sub-scope is a partial result, not a
        # resolution of the whole comparison, unless the sentence speaks overall.
        # The scope may follow the win ("won on accuracy") or precede it. A final
        # round may frame a neutral scope, but it cannot turn a real subset ("on the
        # small sample", "on accuracy") into the overall standing.
        scope = f"{clause} {sentence[match.end() : match.end() + 80]}"
        if _PARTIAL_WIN_SCOPE.search(scope):
            if _LIMITING_WIN_SCOPE.search(scope):
                # An overall frame must belong to this claim. "The final benchmark
                # found A won on the small sample, and overall costs rose" says
                # nothing overall about the comparison.
                overall = _OVERALL_RESULT.search(_claim_window(sentence, clause, match))
            else:
                overall = _OVERALL_MARKER.search(sentence)
            if not overall:
                continue
        # Tie the win to the comparison it claims to settle. A later result must be
        # framed as final/overall/larger/follow-up, or be an explicit winner
        # declaration; otherwise a quiz, a priority list, or a scoped observation
        # would silently cancel the source's open verdict. A directional
        # measurement may also be framed by its own trailing "overall".
        win_scope = f"{clause} {match.group()} {sentence[match.end() : match.end() + 80]}"
        # "In the final test, method A won" and "The final result: method A won"
        # put the result frame in the sentence prefix, before the win's own clause.
        # A semicolon or a contrast starts another claim, and the last contrast
        # segment keeps that claim's frame from settling this win.
        prefix_clause = re.split(r"[;!?]", sentence[:local_start])[-1]
        prefix_scope = _STRONG_CONTRAST.split(prefix_clause)[-1][-80:]
        # "… faster than B overall" frames the measurement itself; "… faster than
        # B, and overall costs rose" does not, so the adverb must follow directly.
        tail_clause = re.split(r"[,;:]", sentence[match.end() : match.end() + 32], maxsplit=1)[0]
        trailing_overall = directional and _OVERALL_RESULT.search(tail_clause) is not None
        if not (
            _UNAMBIGUOUS_WIN.search(win_scope)
            or _LATER_RESULT_FRAME.search(win_scope)
            or _LATER_RESULT_FRAME.search(prefix_scope)
            or _SENTENCE_RESULT_FRAME.search(prefix_scope)
            or trailing_overall
        ):
            continue
        # A contrast that gives the comparison target its own advantage reports a
        # split result ("…faster than B, but B was more accurate"), not a winner.
        if (
            directional
            and not _OVERALL_RESULT.search(_claim_window(sentence, clause, match))
            and _split_opponent_claim(sentence, match)
        ):
            continue
        # A retraction after this win cancels it; a retraction before it targets an
        # earlier claim and does not.
        tail = sentence[match.end() :]
        if sentence_index + 1 < len(spans):
            next_start, next_end = spans[sentence_index + 1]
            tail += " " + text[next_start:next_end]
        if _RETRACTED_CLAIM.search(tail):
            continue
        usable.append((match, _source_at(markers, sentence_start + local_start)))
    return usable


_TARGET_TAIL_STOPWORDS = frozenset(
    "on in for at with under over by during after before when while but and or than vs "
    "versus compared using via per as that which who to from of the a an its their our "
    "his her this these those itself too also overall however although though whereas "
    "yet so because".split()
)


def _directional_target(text: str, match: re.Match[str]) -> frozenset[str]:
    """Tokens naming the alternative a directional comparison measures against.

    The match captures the head of a short name ("than model B" stops at "model"),
    so an immediately following identifier token belongs to the same alternative; a
    preposition or coordinator ends it.
    """
    phrase = re.split(
        r"(?i)\b(?:than|vs\.?|versus|over|against|compared\s+(?:with|to))\b",
        match.group("directional"),
    )[-1]
    tokens = re.findall(r"[\w.-]+", phrase)
    following = re.findall(r"[\w.-]+", text[match.end() :])
    if following and following[0].casefold() not in _TARGET_TAIL_STOPWORDS:
        tokens.append(following[0])
    return frozenset(token.casefold() for token in tokens[-2:])


def _split_opponent_claim(sentence: str, match: re.Match[str]) -> bool:
    """Whether the clause after a directional result hands the target its own edge.

    "A was 40% faster than B, but B was more accurate" and "…, but B cut latency by
    30% over A" report advantages on both sides, so the measurement alone does not
    settle the comparison. Only a claim about the comparison target counts; a
    caveat that still favours the winner ("…, but A was also more accurate") does
    not cancel the result, and neither does a negated or hedged caveat.
    """
    target = _directional_target(sentence, match)
    tail = sentence[match.end() :]
    for claim in _SPLIT_OPPONENT_CLAIM.finditer(tail):
        if _NEGATED_VERDICT.search(f"{claim.group()} "):
            continue
        subject = {token.casefold() for token in re.findall(r"[\w-]+", claim.group("opponent"))}
        if target & subject:
            return True
    for other in _DECISIVE_WIN.finditer(tail):
        group = other.group("directional")
        if group is None or target & _directional_target(tail, other):
            continue
        if _HEDGED_WIN.search(group) or _NEGATED_VERDICT.search(f"{group} "):
            continue
        if _CONTRAST_COORDINATOR.search(tail[: other.start()]) is None:
            continue
        # The opponent must be the side that lost the first measurement.
        subject = tail[max(0, other.start() - 24) : other.start()]
        if target & {token.casefold() for token in re.findall(r"[\w.-]+", subject)}:
            return True
    return False


def _split_directional_claim(sentence: str) -> bool:
    """Whether one sentence reports a directional result and the target's own edge."""
    for match in _DECISIVE_WIN.finditer(sentence):
        if match.group("directional") is None:
            continue
        clause = _clause_before(sentence, match.start())
        if _negated_at(sentence, match.start()) or _HEDGED_WIN.search(sentence):
            continue
        # The negation may sit inside the matched comparison ("was not faster than
        # B"), which the prefix-only check cannot see.
        if _NEGATED_VERDICT.search(f"{clause} {match.group()} "):
            continue
        if _HYPOTHETICAL_WIN.search(clause):
            continue
        # A measurement framed as the overall standing settles the comparison even
        # when the other side has its own advantage ("A was 40% faster than B
        # overall, but B was more stable").
        if _OVERALL_RESULT.search(_claim_window(sentence, clause, match)):
            continue
        if _split_opponent_claim(sentence, match):
            return True
    return False


# A split does not need the "than" target of a directional measurement: "A was
# faster, but B was more stable" names an advantage on each side just as clearly.
# Comparative adjectives that carry an advantage on their own, without "than" and
# without a copula ("Вариант A быстрее", "B was safer").
_COMPARATIVE_EDGE = (
    r"(?:faster|slower|cheaper|quicker|safer|simpler|lighter|smaller|cleaner|"
    r"shorter|leaner|tighter|easier|быстрее|медленнее|дешевле|дороже|точнее|"
    r"стабильнее|надежнее|эффективнее|производительнее|проще|безопаснее|"
    r"швидше|повільніше|дешевше|точніше|стабільніше|надійніше|ефективніше|простіше)"
)
_NON_DIRECTIONAL_EDGE = re.compile(
    r"(?i)(?:"
    rf"\b{_COMPARATIVE_EDGE}\b"
    r"|\b(?:more|less)\s+(?:accurate|precise|efficient|reliable|effective|stable|"
    r"scalable|secure|robust|consistent|responsive)\b"
    r"|\b(?:higher|lower)\s+(?:accuracy|precision|recall|throughput|score|stability|"
    r"quality|latency|cost|memory|runtime|efficiency|reliability|coverage)\b"
    r"|\bfewer\s+(?:errors?|failures?|retries?|requests?|tokens?|bugs?|incidents?)\b"
    r"|\bbetter\s+(?:accuracy|precision|recall|throughput|stability|quality|latency|"
    r"scalability|reliability|efficiency|performance|coverage)\b"
    # A win scoped to one dimension ("won on speed") is a claim about that dimension.
    r"|\b(?:on|in)\s+(?:the\s+)?(?:speed|latency|throughput|stability|accuracy|"
    r"precision|recall|memory|cost|quality|efficiency|reliability|security|"
    r"coverage|runtime|price|energy)\b"
    # Russian and Ukrainian forms after ё folding.
    r"|\bболее\s+(?:точн\w*|стабильн\w*|быстр\w*|надежн\w*|эффективн\w*|"
    r"производительн\w*|дешев\w*)"
    r"|\b(?:выше|ниже)\s+(?:точность|скорость|производительность|стабильность|"
    r"задержк\w*|стоимост\w*|потребление\w*)"
    r")"
)
# A non-directional trade-off needs an anchor that names a comparison: comparison
# or experiment wording, a metric scope, or alternative names ("A", "method B",
# "the first", "the other"). Without one, "the UI is faster, but the API is more
# stable" is a release note about two components, not two sides of one comparison.
_SPLIT_CLAIM_PREDICATE = (
    r"(?:was|were|is|are|had|has|won|wins|beat|beats|outperformed|outperforms|"
    r"used|requires?|took|takes|cut|reduced|delivered|delivers|achieved|"
    r"scored|performed|showed|shows|reported|ran|did|remained|stayed|produced|"
    r"produces|был|была|было|были|имеет|имеют|показал\w*|дает|дал\w*|"
    r"использовал\w*|потребовал\w*|снизил\w*|увеличил\w*|победил\w*|"
    r"обогнал\w*|оказал\w*ся|выиграл\w*|справил\w*ся|був|була|були|має|мають|"
    rf"{_COMPARATIVE_EDGE})"
)
# The anchor may name the alternatives rather than the experiment: "Метод A
# быстрее, но метод B стабильнее" is a comparison even without the word
# "сравнение".
_CYRILLIC_ALTERNATIVE = re.compile(
    r"(?i)\b(?:вариант\w*|метод\w*|подход\w*|модел\w*|алгоритм\w*|систем\w*|"
    r"инструмент\w*|конфигурац\w*|варіант\w*|підхід\w*|модел\w*|систем\w*|"
    r"інструмент\w*)\b"
)
_ALTERNATIVE_SUBJECT = re.compile(
    r"(?i)^(?:the\s+)?(?:first|second|other|former|latter|one|another|third)\b"
    r"|^(?:\w+\s+)?[a-d]$"
)
# A trade-off may also be coordinated with "and" or a semicolon ("A was faster
# and B was cheaper"), unlike a meta question. The anchor check still decides
# whether the two clauses describe alternatives.
_TRADE_OFF_BREAK = re.compile(
    r"(?i)[;]|\b(?:but|however|while|whereas|yet|although|though|and|и|но|однако|"
    r"а|і|але)\b"
)
_PRONOUN_SUBJECT = frozenset(
    {
        "it",
        "its",
        "it's",
        "this",
        "that",
        "these",
        "those",
        "they",
        "он",
        "она",
        "оно",
        "они",
        "это",
        "этот",
        "эта",
        "эти",
        "его",
        "ее",
        "их",
        "він",
        "вона",
        "вони",
        "це",
        "цей",
        "ця",
        "ці",
    }
)
# Words that qualify the claim but do not name the side it is about ("A was also
# more stable" is still about A).
_SUBJECT_FILLER = frozenset(
    {
        "the",
        "an",
        "a",
        "also",
        "both",
        "still",
        "only",
        "even",
        "however",
        "yet",
        "тоже",
        "также",
        "еще",
        "оба",
        "обе",
        "даже",
        "однако",
        "але",
    }
)
_CLAUSE_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)
_HIGHER_IS_BETTER = (
    r"(?:accuracy|precision|recall|throughput|score|stability|quality|efficiency|"
    r"reliability|coverage|performance)"
)
_LOWER_IS_BETTER = (
    r"(?:latency|cost|memory|runtime|price|energy|errors?|failures?|retries?|"
    r"requests?|tokens?|bugs?|incidents?)"
)
# A claim states an advantage for its subject or the opposite. "A was faster and B
# was slower" reports the same finding twice, so the second clause does not give B
# its own edge.
_ADVANTAGE_EDGE = re.compile(
    rf"(?i)^(?:higher|more|better|greater)\s+{_HIGHER_IS_BETTER}$"
    rf"|^(?:lower|less|fewer)\s+{_LOWER_IS_BETTER}$"
    r"|^(?:faster|quicker|cheaper|smaller|lighter|simpler|cleaner|shorter|leaner|"
    r"tighter|safer|easier)$"
    r"|^(?:быстрее|дешевле|точнее|стабильнее|надежнее|эффективнее|"
    r"производительнее|проще|безопаснее)$"
    r"|^более\s+(?:точн|стабильн|быстр|надежн|эффективн|производительн|дешев)\w*$"
    r"|^выше\s+(?:точность|скорость|производительность|стабильность)$"
    r"|^ниже\s+(?:задержк|стоимост|потребление)\w*$"
    r"|^(?:швидше|дешевше|точніше|стабільніше|надійніше|ефективніше|простіше)$"
)
_DISADVANTAGE_EDGE = re.compile(
    rf"(?i)^(?:lower|less|fewer)\s+{_HIGHER_IS_BETTER}$"
    rf"|^(?:higher|more|greater)\s+{_LOWER_IS_BETTER}$"
    r"|^(?:slower|dearer|bigger|heavier|harder|costlier)$"
    r"|^ниже\s+(?:точность|скорость|производительность|стабильность)$"
    r"|^выше\s+(?:задержк|стоимост|потребление)\w*$"
    r"|^(?:медленнее|дороже)$|^повільніше$"
)


def _edge_polarity(edge: str) -> int:
    """``1`` when the claim favours its subject, ``-1`` when it favours the other side."""
    if _ADVANTAGE_EDGE.match(edge):
        return 1
    if _DISADVANTAGE_EDGE.match(edge):
        return -1
    return 0


def _subject_phrase(clause: str) -> str | None:
    """The subject a claim clause attributes its advantage to, when it names one."""
    match = re.search(rf"(?i)\b(?:{_SPLIT_CLAIM_PREDICATE})\b", clause)
    if match is None:
        return None
    phrase = clause[: match.start()].strip(" ,;:")
    # A leading definite article is not part of the name. A one-letter "A" is an
    # alternative name, so it is never stripped as an article.
    phrase = re.sub(r"(?i)^(?:the|an)\s+", "", phrase)
    if not phrase or len(phrase.split()) > 3:
        return None
    return phrase.casefold()


def _subject_key(phrase: str | None) -> frozenset[str] | None:
    """Identity of a claim's subject, ignoring qualifiers such as "also"."""
    if phrase is None:
        return None
    tokens = {token.casefold() for token in _CLAUSE_TOKEN.findall(phrase)}
    meaningful = tokens - _SUBJECT_FILLER
    return frozenset(meaningful or tokens)


def _split_non_directional_claim(sentence: str) -> bool:
    """Whether one sentence reports an advantage on each side without a target.

    "A was faster, but B was more stable", "A won on speed, but B won on stability"
    and "A had higher accuracy, but B had better recall" name no overall winner even
    though neither side has a "than" measurement. A pronoun continuation ("…, but it
    was also cheaper") or a repeated subject ("…, but A was also more stable") is the
    same side's caveat, not the opponent's edge, and a clause that states the other
    side's disadvantage ("…, but B was slower") confirms the first side instead of
    splitting the result.
    """
    if _TRADE_OFF_BREAK.search(sentence) is None:
        return False
    # A directionally measured comparison is decided by the target-aware logic: an
    # "also" caveat about the same side ("A was faster than B, but A was also more
    # accurate") is a confirmation, not the opponent's edge.
    if any(match.group("directional") is not None for match in _DECISIVE_WIN.finditer(sentence)):
        return False
    claims: list[tuple[str | None, list[str], str]] = []
    for clause in _TRADE_OFF_BREAK.split(sentence):
        edges = [
            match.group().casefold()
            for match in _NON_DIRECTIONAL_EDGE.finditer(clause)
            if not _negated_at(clause, match.start())
        ]
        if edges:
            claims.append((_subject_phrase(clause), edges, clause))
    if len(claims) < 2:
        return False
    first_subject, first_edges, first_clause = claims[0]
    second_subject, second_edges, second_clause = claims[1]
    # "A was faster overall, but B was more stable" and "…, but B was more stable
    # overall" frame one of the measured claims itself, so the comparison is decided
    # by that claim rather than left open.
    if _OVERALL_RESULT.search(first_clause) or _OVERALL_RESULT.search(second_clause):
        return False
    second_key = _subject_key(second_subject)
    if second_key is None or second_key & _PRONOUN_SUBJECT:
        return False
    if _subject_key(first_subject) == second_key:
        return False
    # Only the opponent's own advantage splits the result: when its clause only
    # restates the first side's advantage ("B was slower"), nothing is split.
    if not any(_edge_polarity(edge) >= 0 for edge in second_edges):
        return False
    if all(_edge_polarity(edge) < 0 for edge in first_edges) and any(
        _edge_polarity(edge) > 0 for edge in second_edges
    ):
        return False
    return bool(
        _COMPARISON_CONTEXT.search(sentence)
        or re.search(_SCOPE_METRICS, sentence)
        or _CYRILLIC_ALTERNATIVE.search(sentence)
        or _ALTERNATIVE_SUBJECT.search(first_subject or "")
        or _ALTERNATIVE_SUBJECT.search(second_subject or "")
    )


def _split_claim(sentence: str) -> bool:
    return _split_directional_claim(sentence) or _split_non_directional_claim(sentence)


# A comparison or experiment frame. Generic plurals ("samples", "results") are not
# enough: a subset win must belong to a stated comparison to leave it open.
_PARTIAL_RESULT_FRAME = re.compile(
    r"(?i)\b(?:comparisons?|compared|versus|vs\.?|benchmarks?|trials?|studies|study|"
    r"experiments?|evaluations?|analys[ie]s|rounds?|runs?|replications?|follow-ups?|"
    r"tests?)\b"
)


def _has_unframed_partial_win(sentence: str, following: str = "") -> bool:
    """Whether a sentence reports a win only for a named subset of the comparison.

    "The final benchmark found A won on the small sample" does not establish the
    overall standing: the win is scoped to a subset and no part of its own claim
    says "overall". Such a sentence leaves the comparison unresolved instead of
    settling it. Rejected mentions (negated, hedged, hypothetical, separate
    experiment) and a retracted result cannot open the comparison either.
    """
    if not _PARTIAL_RESULT_FRAME.search(sentence):
        return False
    for match in _DECISIVE_WIN.finditer(sentence):
        clause = _clause_before(sentence, match.start())
        if _win_mention_rejected(sentence, clause, match):
            continue
        if _RETRACTED_CLAIM.search(f"{sentence[match.end() :]} {following}"):
            continue
        scope = f"{clause} {sentence[match.end() : match.end() + 80]}"
        if not _LIMITING_WIN_SCOPE.search(scope):
            continue
        if _OVERALL_RESULT.search(_claim_window(sentence, clause, match)):
            continue
        return True
    return False


def _split_comparison_result(
    text: str,
    spans: list[tuple[int, int]],
    markers: list[tuple[int, int]],
) -> bool:
    """Whether an unresolved comparison reports advantages on both sides.

    "A was 40% faster than B, but B was 30% cheaper" names no overall winner, so the
    comparison is unresolved even without the word "inconclusive". A decisive result
    that follows the split still settles it, so the open claim is positional: a
    final win before the split ("A beat B in the final benchmark. Later, A was 40%
    faster than B, but B was more accurate") does not cancel the later trade-off,
    while the same win after it does. A win scoped to a subset of a named comparison
    or experiment without an overall frame ("the final benchmark found A won on the
    small sample") leaves the same question open.
    """
    open_claims: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(spans):
        sentence = text[start:end]
        source = _source_at(markers, start)
        if _split_claim(sentence):
            open_claims.append((index, source))
            continue
        following = (
            text[spans[index + 1][0] : spans[index + 1][1]] if index + 1 < len(spans) else ""
        )
        if _has_unframed_partial_win(sentence, following):
            open_claims.append((index, source))
    if not open_claims:
        return False
    wins_by_sentence = [
        _usable_wins_in_sentence(text, spans, index, markers) for index in range(len(spans))
    ]
    decisive = _decisive_sentence_indexes(text, spans, wins_by_sentence)
    for index, source in open_claims:
        later = decisive.get(source, [])
        if bisect_right(later, index) >= len(later):
            return True
    return False


def _is_split_win(sentence: str, wins: list[re.Match[str]]) -> bool:
    """Whether one sentence reports wins in different scopes ("A won X, but B won Y").

    Coordinated wins with no overall framing describe a split result, not a
    decisive outcome; they must not cancel an explicit no-winner verdict.
    """
    if len(wins) < 2 or _OVERALL_MARKER.search(sentence):
        return False
    between = sentence[wins[0].start() : wins[-1].end()]
    return _CONTRAST_COORDINATOR.search(between) is not None


def _decisive_sentence_indexes(
    text: str,
    spans: list[tuple[int, int]],
    wins_by_sentence: list[list[tuple[re.Match[str], int]]],
) -> dict[int, list[int]]:
    """Sentence indexes per source that state a non-split decisive win.

    Precomputing this keeps a document with many verdict mentions linear instead of
    rescanning every later sentence for each one.
    """
    indexes: dict[int, list[int]] = {}
    for index, wins in enumerate(wins_by_sentence):
        by_source: dict[int, list[re.Match[str]]] = {}
        for match, source in wins:
            by_source.setdefault(source, []).append(match)
        sentence = text[spans[index][0] : spans[index][1]]
        for source, matches in by_source.items():
            if _is_split_win(sentence, matches):
                continue
            indexes.setdefault(source, []).append(index)
    return indexes


def _has_later_decisive_win(
    text: str,
    spans: list[tuple[int, int]],
    wins_by_sentence: list[list[tuple[re.Match[str], int]]],
    decisive_indexes: dict[int, list[int]],
    index: int,
    floor: int,
    mention_source: int,
) -> bool:
    """Whether a sentence at or after ``index`` states a decisive comparison result.

    ``floor`` is the end of the verdict mention: a win stated before the verdict in
    the same sentence ("although A won the small sample, the comparison was
    inconclusive") does not supersede the verdict that follows it.
    """
    sentence_start, sentence_end = spans[index]
    own = [
        match
        for match, source in wins_by_sentence[index]
        if source == mention_source and sentence_start + match.start() >= floor
    ]
    if own and not _is_split_win(text[sentence_start:sentence_end], own):
        return True
    later = decisive_indexes.get(mention_source)
    if not later:
        return False
    return bisect_right(later, index) < len(later)


_NULL_DIFFERENCE = re.compile(
    r"(?i)^no\s+(?:meaningful|significant|clear|measurable|statistically\s+significant)?"
    r"\s*difference\b"
)
# "Mixed results are common in A/B testing" describes a general phenomenon, not
# this source's verdict. The predicate must follow the phrase so "the results were
# mixed because the samples differed" stays a result.
_GENERIC_PHENOMENON = re.compile(
    r"(?i)\b(?:mixed|inconclusive|contradictory|conflicting)\s+"
    r"(?:results?|findings?)\s+(?:are|were|is|was)\s+"
    r"(?:common|typical|expected|normal|usual|frequent|likely|rare|widespread|"
    r"unsurprising)\b"
)
# Generic Russian/Ukrainian outcome nouns describe uncertainty about anything
# ("результат лечения", "результат анализа крови") unless the sentence names the
# comparison or experiment that produced them.
_CYRILLIC_GENERIC_RESULT = re.compile(
    r"(?i)\b(?:результат\w*|вывод\w*|итог\w*|исход\w*|результат\w*|"
    r"виснов\w*|підсум\w*)\b"
)
_CYRILLIC_COMPARISON_CONTEXT = re.compile(
    r"(?i)\b(?:сравнени\w*|сопоставлен\w*|эксперимент\w*|исследован\w*|"
    r"испытан\w*|тест\w*|замер\w*|метод\w*|подход\w*|вариант\w*|модел\w*|"
    r"алгоритм\w*|конфигурац\w*|систем\w*|инструмент\w*|"
    r"порівнян\w*|експеримент\w*|досліджен\w*|випробуван\w*|тест\w*|"
    r"замір\w*|метод\w*|підхід\w*|варіант\w*|модел\w*|алгоритм\w*|"
    r"A/B|а/б)\b"
)
# "The results were mixed" is this source's verdict when the sentence names the
# comparison or experiment that produced it. The generic result nouns are excluded
# from the subject list, otherwise every verdict sentence would qualify itself.
_COMPARISON_SUBJECT = re.compile(
    r"(?i)\b(?:comparisons?|compared|versus|vs\.?|benchmarks?|trials?|studies|study|"
    r"experiments?|tests?|evaluations?|analys[ie]s|rounds?|runs?|replications?|"
    r"follow-ups?|methods?|approaches?|models?|systems?|tools?|strategies|"
    r"strategy|algorithms?|techniques?|baselines?|variants?|options?|"
    r"configurations?|agents?|prompts?|samples?|datasets?|workloads?)\b"
)
# A domain scope is what makes an English generic result unrelated: "mixed across
# industries" and "survey results were mixed" describe a population, not two
# alternatives measured against each other.
_DOMAIN_SCOPE = re.compile(
    r"(?i)\b(?:surveys?|polls?|questionnaires?|respondents|industr(?:y|ies)|"
    r"sectors?|regions?|countries|markets?|consumers|customers|demographics?|"
    r"elections?|votes?)\b"
)
_ENGLISH_GENERIC_RESULT = re.compile(r"(?i)\b(?:results?|findings?|outcomes?)\b")


def _unrelated_null_difference(
    text: str, spans: list[tuple[int, int]], sentence_index: int, match: re.Match[str]
) -> bool:
    """Whether "no difference" describes an unrelated attribute, not a comparison.

    A measured attribute alone ("no difference in time zones between servers") is
    not a comparison of approaches. The sentence must name a comparison, experiment,
    method, or alternative for the null result to be a verdict.
    """
    if not _NULL_DIFFERENCE.search(match.group()):
        return False
    sentence_start, sentence_end = spans[sentence_index]
    sentence = text[sentence_start:sentence_end]
    return (
        _COMPARISON_CONTEXT.search(sentence) is None
        and _CYRILLIC_COMPARISON_CONTEXT.search(sentence) is None
    )


def _unrelated_generic_result(
    text: str, spans: list[tuple[int, int]], starts: list[int], match: re.Match[str]
) -> bool:
    """Whether a generic outcome noun describes something other than a comparison.

    "Результат лечения был неопределённым" is uncertainty about a treatment, not a
    no-winner verdict. "Результат сравнения был неопределённым" names the comparison
    and keeps the verdict. An English generic result needs both a non-comparison
    domain ("across industries", "survey results") and no comparison subject; a bare
    "the results were mixed" stays the source's verdict.
    """
    sentence_index = _sentence_at(spans, starts, match.start())
    if sentence_index is None:
        return False
    sentence_start, sentence_end = spans[sentence_index]
    sentence = text[sentence_start:sentence_end]
    if _CYRILLIC_GENERIC_RESULT.search(match.group()):
        return _CYRILLIC_COMPARISON_CONTEXT.search(sentence) is None
    if _ENGLISH_GENERIC_RESULT.search(match.group()):
        return (
            _COMPARISON_SUBJECT.search(sentence) is None
            and _DOMAIN_SCOPE.search(sentence) is not None
        )
    return False


def _background_verdict(
    text: str,
    spans: list[tuple[int, int]],
    starts: list[int],
    match: re.Match[str],
) -> bool:
    """Whether a verdict phrase only describes prior work or a general phenomenon."""
    sentence_index = _sentence_at(spans, starts, match.start())
    if sentence_index is None:
        return _PRIOR_WORK.search(_clause_before(text, match.start())) is not None
    sentence_start, sentence_end = spans[sentence_index]
    # A generic statement ("mixed results are common in A/B testing") describes a
    # phenomenon, not this source's verdict. Only the first clause after the match
    # is inspected, so a separate specific verdict joined by a semicolon stays.
    clause_tail = re.split(r"[.;:,!?]", text[match.end() : sentence_end], maxsplit=1)[0]
    if _GENERIC_PHENOMENON.search(f"{match.group()}{clause_tail}"):
        return True
    # The meta phrase may sit in an earlier clause of the same sentence
    # ("объясняется, почему …"), but it must introduce this exact clause.
    prefix = text[sentence_start : match.start()]
    if _PRIOR_WORK.search(_clause_before(text, match.start())):
        return True
    # A meta verb may introduce this clause across a comma ("объясняется,
    # почему …"), but it must not reach across a contrast or a semicolon.
    meta_tail = prefix.rsplit(";", 1)[-1]
    meta_scope = _CONTRAST_COORDINATOR.split(meta_tail)[-1]
    if _META_WHY_BEFORE.search(meta_scope):
        return True
    # "explains how X works and why mixed results are common" keeps both embedded
    # questions under the explanatory verb; the second question must be the last
    # clause, so a following main claim is not swallowed by the same verb.
    strong_scope = _STRONG_CONTRAST.split(meta_tail)[-1]
    if _META_VERB.search(strong_scope) and _META_COORDINATED.search(strong_scope):
        return True
    return _PRIOR_WORK_AFTER.search(text[match.end() : sentence_end]) is not None


def _verdict_mentions(text: str) -> list[tuple[re.Match[str], bool]]:
    """Explicit no-winner mentions in text order; the flag marks insufficiency forms."""
    mentions = [(match, False) for match in _INCONCLUSIVE_OUTCOME.finditer(text)]
    mentions += [(match, True) for match in _INSUFFICIENCY_VERDICT.finditer(text)]
    mentions.sort(key=lambda mention: mention[0].start())
    return mentions


def _named_alternative_labels(sentence: str) -> set[str]:
    """Labels of the alternatives the sentence names ("method A", "вариант 1")."""
    labels = set()
    for noun in _NAMED_ALTERNATIVE.finditer(sentence):
        run = _ALTERNATIVE_LABEL_RUN.match(sentence[noun.end() :])
        if run is not None:
            labels.update(token.casefold() for token in _ALTERNATIVE_LABEL.findall(run.group()))
    return labels


# The purpose's claim clause ends at a sentence break, a colon, a spaced dash, a
# comma with a coordinator, or a coordinator that introduces a new subject
# ("…cause and the team asked which one is better"). A coordinated noun phrase
# ("between option A and option B") has no subject and stays inside the purpose.
_COMPARISON_PURPOSE_BOUNDARY = re.compile(
    r"(?i)[.;!?\n]|:\s|\s[—–-]\s"
    r"|,\s*(?:and|but|or|while|whereas|yet|however|then|so|и|но|а|однако|затем|поэтому)\b"
    r"|\b(?:and|but|or|yet|so)\s+(?:the|a|an|this|that|these|those|there|we|they|it|"
    r"he|she|i|you|our|their|its|his|her)\b"
    r"|\b(?:и|но|а|однако|затем|поэтому)\s+(?:команда|исследовател\w*|автор\w*|"
    r"они|она|он|оно|это|эти|мы|вы|пользовател\w*|заказчик\w*)\b"
)


def _insufficiency_purpose(text: str, verdict: re.Match[str], sentence_end: int) -> str:
    """The complement that scopes the missing data ("the cause", "a winner")."""
    # A purpose complement is a short clause. Bounding the tail keeps a verdict-heavy
    # long sentence linear instead of rescanning the rest of the sentence per match.
    tail_end = min(sentence_end, verdict.end() + _CLAUSE_WINDOW)
    tail = text[verdict.end() : tail_end]
    purpose = _INSUFFICIENCY_PURPOSE_PREFIX.sub("", tail)
    # A coordinated clause after the purpose is a separate claim: "to determine the
    # cause, and the team asked whether …" must not lend its comparison to the
    # purpose. A comma inside the purpose phrase ("a clear, single winner") stays.
    return _COMPARISON_PURPOSE_BOUNDARY.split(purpose, maxsplit=1)[0].strip()


def _purpose_selects_a_comparison_outcome(purpose: str) -> bool:
    """Whether the purpose itself selects the outcome, not an attribute of one.

    A comparison noun nested in a possessive, causal, explanatory or measurement
    phrase ("the winner's margin", "why the winning approach worked", "the cause of
    the winning approach's advantage") describes an already-established outcome, so
    the missing data is not about which side won.
    """
    for match in _INSUFFICIENCY_COMPARISON_OUTCOME.finditer(purpose):
        if _ATTRIBUTIVE_OUTCOME_TAIL.match(purpose, match.end()):
            continue
        if _ATTRIBUTIVE_PURPOSE_HEAD.search(purpose[: match.start()]):
            continue
        return True
    return False


def _insufficiency_names_a_comparison(
    text: str,
    spans: list[tuple[int, int]],
    sentence_index: int | None,
    verdict: re.Match[str],
) -> bool:
    """Whether "not enough data" is about the outcome of a real comparison.

    The missing data must be scoped to the comparison outcome ("pick a winner",
    "which one is better"). A comparison mentioned elsewhere in the sentence — an
    explanatory "explains how a comparison works", a temporal "comparison with last
    year", a metric inside "the cause of the improvement in accuracy" — states
    ordinary uncertainty about a fact, not a no-winner verdict. When the phrase has
    no purpose complement ("…; данных недостаточно."), the verdict stands only when
    the same sentence names two concrete alternatives.
    """
    if sentence_index is None:
        return False
    purpose = _insufficiency_purpose(text, verdict, spans[sentence_index][1])
    if purpose:
        return _purpose_selects_a_comparison_outcome(purpose)
    start, end = spans[sentence_index]
    return len(_named_alternative_labels(text[start:end])) >= 2


def _explicit_verdict(
    text: str,
    spans: list[tuple[int, int]],
    markers: list[tuple[int, int]],
) -> bool | None:
    """Whether an explicit no-winner verdict stands.

    ``True`` when one survives, ``False`` when a later decisive win supersedes a
    verdict the evidence actually stands behind, and ``None`` when no current
    verdict stands: only background, negated, retracted, or unrelated mentions. The
    distinction matters because ``False`` settles the comparison, while a mention
    that never states this source's verdict must still let the split inference run.
    """
    starts = [start for start, _ in spans]
    wins_by_sentence: list[list[tuple[re.Match[str], int]]] | None = None
    decisive_indexes: dict[int, list[int]] = {}
    superseded = False
    for match, needs_comparison in _verdict_mentions(text):
        if _negated_at(text, match.start()):
            continue
        # The negation may be part of the matched phrase ("were not inconclusive").
        if _NEGATED_INCONCLUSIVE.search(_clause_before(text, match.start()) + match.group()):
            continue
        # "В тендере победителя нет" and "no winner was announced in the election"
        # name an event, not a method comparison.
        if _event_scoped_verdict(text, spans, starts, match):
            continue
        sentence_index = _sentence_at(spans, starts, match.start())
        # "There is not enough evidence to determine the cause" is uncertainty about a
        # fact; the phrase is a comparison verdict only when the missing data is
        # scoped to a comparison outcome ("pick a winner").
        if needs_comparison and not _insufficiency_names_a_comparison(
            text, spans, sentence_index, match
        ):
            continue
        if _background_verdict(text, spans, starts, match):
            # "Prior studies reported mixed results" or "the article explains why
            # mixed results are common" describes the literature or the topic; it
            # is not the current source's verdict.
            continue
        if _RETRACTED_VERDICT.search(text[match.end() :]):
            continue
        if sentence_index is None:
            return True
        if _unrelated_null_difference(text, spans, sentence_index, match):
            continue
        if _unrelated_generic_result(text, spans, starts, match):
            continue
        if wins_by_sentence is None:
            wins_by_sentence = [
                _usable_wins_in_sentence(text, spans, index, markers) for index in range(len(spans))
            ]
            decisive_indexes = _decisive_sentence_indexes(text, spans, wins_by_sentence)
        if _has_later_decisive_win(
            text,
            spans,
            wins_by_sentence,
            decisive_indexes,
            sentence_index,
            match.end(),
            _source_at(markers, match.start()),
        ):
            superseded = True
            continue
        return True
    return False if superseded else None


def states_inconclusive_comparison(text: str) -> bool:
    """Whether the captured evidence leaves an unresolved comparison.

    A negated mention ("the results were not inconclusive") cancels only itself.
    A later decisive win supersedes the verdict only when it belongs to the same
    comparison and source: a hypothetical ("whether A outperformed B"), a hedged
    possibility, a retracted win, a split win in another scope, or a win in a
    separate experiment must not turn an open comparison into a settled one. A
    verdict that only reports prior work or explains a phenomenon is background,
    not this source's conclusion. An explicitly retracted inconclusive conclusion
    is also not a verdict the source still stands behind. A comparison that reports
    an advantage on each side without naming a winner is unresolved even when the
    word "inconclusive" never appears, and a trade-off stated after a decisive win
    reopens the comparison that the earlier result appeared to settle.
    """
    folded = _fold(text)
    spans = _sentence_spans(folded)
    markers = _source_markers(folded)
    explicit = _explicit_verdict(folded, spans, markers)
    if explicit is True:
        return True
    # A later trade-off outlives an earlier decisive win ("A beat B in the final
    # benchmark. Later, A was 40% faster than B, but B was more accurate"), so the
    # split inference runs even when the verdict itself was superseded.
    return _split_comparison_result(folded, spans, markers)


def has_multiple_sources(metadata: Mapping[str, object]) -> bool:
    """Whether more than one source actually contributed evidence.

    ``source_count`` includes sources whose extraction failed. A single available
    source must still receive the no-winner hint and repair even when the Item was
    composed from several requested sources.
    """
    successful = metadata.get("successful_source_count")
    if type(successful) is int:
        return successful > 1
    count = metadata.get("source_count")
    if type(count) is not int:
        return False
    failures = metadata.get("source_failures")
    if isinstance(failures, list):
        return count - len(failures) > 1
    return count > 1


def title_states_inconclusive(title: str) -> bool:
    """Whether a title already states the verdict; the suffix is then a no-op.

    A title that only lists both measured results is not a verdict: the repair
    exists to add the explicit no-winner statement, so the split inference is not
    used here.
    """
    folded = _fold(title)
    if _explicit_verdict(folded, _sentence_spans(folded), _source_markers(folded)) is True:
        return True
    folded_case = folded.casefold()
    return any(
        _fold(suffix.removeprefix("; ")).casefold() in folded_case
        for suffix in _INCONCLUSIVE_SUFFIX.values()
    )


def _language_hint(text: str) -> str | None:
    """Recognize the languages the repair can write without importing a detector."""
    if _UKRAINIAN.search(text):
        return "uk"
    if _CYRILLIC.search(text):
        return "ru"
    # Two markers avoid treating an English noun such as "die casting" as German.
    if len(_GERMAN.findall(text)) >= 2:
        return "de"
    if len(_SPANISH.findall(text)) >= 2:
        return "es"
    if len(_FRENCH.findall(text)) >= 2:
        return "fr"
    if _ENGLISH.search(text):
        return "en"
    return None


def inconclusive_title_language(
    title: str, response_language: str | None = None, evidence: str = ""
) -> str | None:
    """The language the repair will write the verdict in; ``None`` means none.

    The title script and evidence wording are only a fallback for callers without
    profile context; the title wins so a Russian answer keeps a Russian suffix even
    beside English evidence. When no response language or recognizable translation
    is available, return no language rather than inserting English into another
    language's title. The same language also selects the coordination, negation and
    function words the title trim may rely on, so Spanish "en" is never read as the
    Dutch coordinator "en".
    """
    language = (response_language or "").split("-")[0].casefold()
    if language in _INCONCLUSIVE_SUFFIX:
        return language
    if language:
        return None
    hint = _language_hint(title)
    if hint == "ru" and _UKRAINIAN.search(evidence):
        # A Ukrainian title can look Russian until its text provides a marker;
        # the evidence disambiguates the shared Cyrillic script.
        hint = "uk"
    if hint is None:
        hint = _language_hint(evidence)
    return hint if hint in _INCONCLUSIVE_SUFFIX else None


def inconclusive_title_suffix(
    title: str, response_language: str | None = None, evidence: str = ""
) -> str:
    """Pick the suffix for the actual response language."""
    language = inconclusive_title_language(title, response_language, evidence)
    return _INCONCLUSIVE_SUFFIX[language] if language else ""
