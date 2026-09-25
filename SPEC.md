# Спецификация: генератор лендингов из выгрузки Google Search Console

Документ фиксирует контракты, по которым пишется код. Если в процессе реализации выясняется,
что спецификация неверна, — сначала правится спецификация, потом код.

Источник требований: `ЗАДАНИЕ.md` (тестовое задание Icons8, SEO Developer).

---

## 1. Задача и границы

**На входе:** CSV-выгрузка запросов из Google Search Console по icons8.com.
**На выходе:** ~10 самодостаточных HTML-лендингов, связанных между собой, плюс машинный отчёт
о принятых решениях.

**Обязательное свойство:** скрипт отрабатывает на другой выгрузке из той же консоли без
правки кода. Всё, что может меняться (бренд, пороги, целевое число страниц, ID моделей,
тексты промптов), лежит в `config/`, а не в коде.

**Вне рамок:** деплой, реальные ассеты каталога, `sitemap.xml`, дизайн.

---

## 2. Стадии пайплайна и их контракты

```
CSV → [ingest] → list[Query]
    → [normalize] → list[Query] (дедуп, нормализованный text)
    → [filters]  → list[Decision]            # детерминированные правила
    → [classify] → list[Decision]            # LLM-1, уточняет решения
    → [group]    → list[LexicalGroup]        # только keep=True
    → [cluster]  → list[Cluster]             # LLM-2, ровно target±tolerance
    → [content]  → list[Cluster] + PageContent  # LLM-3, на кластер
    → [render]   → out/index.html, out/<slug>.html
    → [report]   → out/report.json, out/excluded.csv
```

Каждая стадия — чистая функция от входных данных и конфига. Стадия не обращается к
файловой системе, кроме `ingest` (чтение), `render`/`report` (запись) и `llm.cache`.

---

## 3. Модели данных (`domain/models.py`)

```python
Intent = Literal["transactional", "informational", "navigational"]

@dataclass(frozen=True)
class Query:
    raw: str              # строка из CSV без изменений
    text: str             # lower, trim, пробелы свёрнуты
    clicks: int
    impressions: int
    ctr: float            # доля 0..1
    position: float | None  # None, если в выгрузке нет или 0

@dataclass
class Decision:
    query: Query
    keep: bool
    reason: Reason
    note: str                              # ≤120 символов, человекочитаемо
    source: Literal["rule", "llm", "default"]
    intent: Intent | None = None

@dataclass
class LexicalGroup:
    key: str                # сигнатура из нормализованных токенов
    canonical: str          # запрос группы с максимальными кликами
    queries: list[Query]

@dataclass
class Cluster:
    slug: str
    primary_keyword: str
    intent: Intent
    groups: list[LexicalGroup]
    source: Literal["llm", "lexical"]
    content: PageContent | None = None

@dataclass
class PageContent:
    title: str             # ≤60
    meta_description: str  # ≤155
    h1: str
    intro: str
    sections: list[Section]        # Section: heading: str, paragraphs: list[str]
    faq: list[FaqItem]            # FaqItem: question: str, answer: str
    anchor_text: str              # анкор для ссылок с других страниц
    source: Literal["llm", "template"]
```

Производные метрики (`domain/scoring.py`, не хранятся в полях): `clicks`, `impressions`,
`avg_position` (среднее по позициям, взвешенное по показам), `potential`.

---

## 4. Причины исключения (`domain/reasons.py`)

Перечень **закрытый**. Это же значения колонки `reason` в `excluded.csv` и ключи сводки в
`report.json`.

| reason | когда | пример из выгрузки |
|---|---|---|
| `ok` | запрос проходит дальше | `free icons` |
| `own_brand` | навигационный запрос своего бренда | `icons8`, `icon8. com`, `8icons` |
| `third_party_brand` | чужая торговая марка или связанный с ней символ | `instagram logo`, `apple logo copy`, `instagram blue tick copy`, `shein` |
| `non_english` | запрос не на английском | `kursor myszki do pobrania` |
| `not_a_landing_intent` | интент не для страницы каталога (блог, how-to, софт-хак) | `graphic design trends 2027`, `tiktok blue tick injector` |
| `irrelevant` | к каталогу дизайн-ассетов не относится | `faceswapper` |
| `duplicate` | склеен с другим запросом при дедупе | `Free Icons` → `free icons` |
| `low_volume` | ниже порога `input.min_impressions` | — |
| `truncated` | не попал в `--max-queries` | — |
| `unclustered` | прошёл фильтры, но не попал в целевые `target_pages` кластеров | — |

`own_brand` и `third_party_brand` отключаются флагами `--keep-brand` и
`--keep-third-party`: запрос остаётся в работе, а в отчёт пишется `ok` с пометкой
`note="kept by flag"`.

---

## 5. Формула потенциала (`domain/scoring.py`)

Кластеры ранжируются по потенциалу роста, а не по текущим кликам: лендинг имеет смысл там,
где показы есть, а позиция низкая.

```
opportunity(position) = clamp((position - 3.0) / 7.0, 0.0, 1.0)   # 0 при топ-3, 1 при позиции ≥10
opportunity(None)     = 0.5                                       # позиции в выгрузке нет
potential(query)      = impressions * opportunity(position)
potential(cluster)    = Σ potential(query)
```

Сортировка кластеров: `potential` ↓, затем `impressions` ↓, затем `clicks` ↓, затем `slug` ↑.
Последний ключ гарантирует детерминированный порядок при равенстве.

---

## 6. `ingest/` — чтение выгрузки

**Вход:** путь к `.csv` или `.zip` (в архиве берётся первый `*.csv`).

Правила чтения:
1. Кодировка `utf-8-sig` (BOM снимается), при ошибке — `cp1251`.
2. Разделитель определяется `csv.Sniffer` по первой строке; кандидаты `, ; \t`.
3. Заголовок приводится к нижнему регистру, обрезаются пробелы и `(%)`, затем ищется по
   словарю синонимов (`ingest/headers.py`):
   - `query`: `query`, `queries`, `top queries`, `search query`, `запрос`, `запросы`, `самые популярные запросы`
   - `clicks`: `clicks`, `клики`, `кликов`
   - `impressions`: `impressions`, `показы`, `показов`
   - `ctr`: `ctr`, `кликабельность`
   - `position`: `position`, `average position`, `avg position`, `позиция`, `средняя позиция`
4. Порядок колонок произвольный, лишние колонки игнорируются.
5. Обязательна только колонка запроса. Нет колонки метрики → значение `0` (`position` → `None`)
   и предупреждение в отчёт.
6. Пустые строки и строки с пустым запросом пропускаются.
7. Загрузчик ничего не отбрасывает: ограничение `--max-queries` применяется после дедупа
   (§7), чтобы усечение считалось по итоговым метрикам, а не по разбитым на дубли.

**Разбор чисел (`ingest/numbers.py`):**
- убираются пробелы, неразрывные пробелы, символ `%` (с флагом «было процентом»);
- если в числе есть и `.`, и `,` → десятичным считается тот разделитель, который стоит
  правее (`26,306.5` → 26306.5; `1.234,56` → 1234.56), второй — разделитель тысяч;
- если есть только `,`: ровно одна запятая, после неё ровно 3 цифры, перед ней 1–3 цифры →
  разделитель тысяч (`26,306` → 26306); иначе — десятичная запятая (`0,5495` → 0.5495);
- несколько запятых → разделитель тысяч (`1,234,567`);
- пустая строка, `-`, `—`, `n/a` → `0`;
- нераспознанное значение → `0` и предупреждение.

**Постобработка CTR и позиции:** если после разбора `ctr > 1` — значение считается процентом
и делится на 100. `position == 0` → `None`.

**Ошибки:** если колонка запроса не найдена — `InputError` с сообщением, перечисляющим
фактически найденные заголовки. Код выхода `1`. Трейсбек не показывается.

---

## 7. `pipeline/normalize.py`

**Нормализация запроса:** lower → свёртка пробелов → удаление лишней пунктуации по краям.
Дедуп по `text`: метрики суммируются (`clicks`, `impressions`), `position` усредняется с
весом показов, `ctr` пересчитывается как `clicks / impressions`. Поглощённые дубли попадают в
отчёт с `reason=duplicate`.

**Лексический ключ группы:** lower → удалить не-`[a-z0-9 ]` → токенизация → отбросить
стоп-слова (`for`, `to`, `the`, `a`, `of`, `and`, `my`, `best`, `online`) → упрощённая
синуляризация (`-ies`→`y`, `-s` кроме `-ss`/`-us`) → сортировка токенов → соединение через `-`.

Примеры: `free icons`, `free icon`, `icons free`, `icons free download`, `download free icons`
→ ключи `free-icon` / `download-free-icon`. Полного склеивания лексика не даёт — остальное
делает стадия кластеризации.

**Опечатки:** группы с расстоянием Левенштейна ≤1 по ключу при длине ключа ≥6 склеиваются
(`custom-curser` → `custom-cursor`). Опечатка не исключается, а становится вариантом внутри
группы.

---

## 8. `pipeline/filters.py` — правила до LLM

Дешёвые и объяснимые проверки, дающие `Decision(source="rule")`:

1. **Свой бренд.** Запрос нормализуется в `[a-z0-9]` (пробелы и пунктуация удаляются) и
   сравнивается с нормализованными алиасами из `config.brand.aliases`. Совпадение целиком или
   расстояние Левенштейна ≤ `config.brand.fuzzy_distance` → `own_brand`. Одно правило
   покрывает `icons8`, `icon8`, `icons 8`, `8icons`, `icon8. com`, `icon 8`.
   Если запрос содержит алиас **плюс** значимые слова (`icons8 background remover`), он тоже
   `own_brand`: это навигационный запрос к конкретному продукту бренда.
2. **Язык.** Наличие символов вне Latin-1 или совпадение со стоп-словами ru/pl/de/es
   (`do`, `pobrania`, `myszki`, `бесплатно`, `descargar`, `kostenlos`, …) → `non_english`.
3. **Мусор.** Пустой запрос, длина <2, чистый URL или домен → `irrelevant`.
4. **Порог объёма.** `impressions < config.input.min_impressions` → `low_volume` (по умолчанию
   порог 0, то есть правило выключено).

Правила не принимают решений, требующих суждения (чужой бренд, интент) — это стадия LLM.
В офлайн-режиме их подменяет эвристика из `config.offline_markers` (список токенов вроде
`instagram`, `apple`, `tiktok`, `blue tick`, `copy and paste`, `injector`, `emoji`), о чём в
отчёт пишется предупреждение: офлайн-классификация грубее.

---

## 9. `pipeline/classify.py` — LLM-1 (Haiku 4.5)

Батчи по `config.llm.batch_size` (по умолчанию 50) запросов, `temperature=0`, structured
output через tool use.

**Что уходит в модель:** текст запроса, `clicks`, `impressions`, `position`, рынок (`en`),
описание бизнеса (каталог дизайн-ассетов) и перечень причин из §4. Промпт — `config/prompts/classify.md`.

**Схема ответа:**
```json
{"items": [{"query": "string",
            "keep": true,
            "reason": "ok|own_brand|third_party_brand|non_english|not_a_landing_intent|irrelevant",
            "intent": "transactional|informational|navigational",
            "note": "string ≤120"}]}
```

**Валидация и слияние с правилами:**
- решение правила приоритетнее решения LLM для `own_brand` и `non_english` (они объективны);
- запрос отсутствует в ответе → `keep=true, reason=ok, source="default"` + предупреждение;
- `reason` вне перечня → `irrelevant` при `keep=false`, `ok` при `keep=true` + предупреждение;
- `keep=false, reason=ok` → трактуется как `irrelevant`.

---

## 10. `pipeline/cluster.py` — LLM-2 (Sonnet 5)

В модель уходят **лексические группы, а не сырые запросы**: меньше токенов и меньше шума.
На группу передаются `key`, `canonical`, до 5 входящих запросов, суммарные `clicks`,
`impressions`, `avg_position`, `potential`, преобладающий `intent`.

**Схема ответа:**
```json
{"clusters": [{"primary_keyword": "string",
               "slug": "string",
               "intent": "transactional|informational|navigational",
               "group_keys": ["string"],
               "rationale": "string ≤200"}]}
```

**Правила приведения к цели (детерминированно, после LLM):**
- неизвестный `group_keys` игнорируется + предупреждение;
- группа, не попавшая ни в один кластер, присоединяется к кластеру с максимальным
  пересечением токенов; при нулевом пересечении образует свой кластер;
- кластеры с числом запросов < `config.pages.min_queries_per_cluster` сливаются с ближайшим
  по токенам;
- кластеры сортируются по §5; берутся `config.pages.target` штук, остальные — в отчёт с
  `reason=unclustered`;
- допустимое отклонение от цели — `config.pages.tolerance` (±2): если после слияний
  кластеров меньше, добираем из `unclustered` по потенциалу.

**Slug:** из `primary_keyword`: lower → `[^a-z0-9]+` → `-` → обрезка до 60 символов по границе
слова. Коллизия → суффикс `-2`, `-3`. Slug стабилен при неизменном `primary_keyword`.

**Фолбэк (`--no-llm` или ошибка API):** кластер = лексическая группа; `primary_keyword` =
`canonical`; `intent` — по эвристике (`download`/`free` → transactional, `what`/`how` →
informational); берутся топ-`target` по потенциалу.

---

## 11. `pipeline/content.py` — LLM-3 (Sonnet 5)

Один вызов на кластер. В модель уходят: `primary_keyword`, все запросы кластера с метриками,
`intent`, названия остальных страниц набора (для перелинковки), язык — английский.
Промпт — `config/prompts/content.md`.

**Схема ответа:** соответствует `PageContent` (§3). Ограничения в схеме и в промпте:
`title` ≤60, `meta_description` ≤155, `sections` 3–4, `paragraphs` 1–2 на секцию,
`faq` 4–6 (вопросы формулируются из реальных запросов кластера), `anchor_text` 2–5 слов.

**Запрещено:** придумывать количество ассетов, цены, названия тарифов, отзывы, даты.
Числовые факты не изобретаются — в шаблоне для них плейсхолдеры.

**Валидация:** превышение лимитов → обрезка по границе слова + предупреждение; меньше 3
секций или 4 FAQ → добор шаблонными блоками из запросов кластера; наличие числа из 3+ цифр в
тексте → предупреждение в отчёт (вероятный выдуманный факт); невалидный JSON → один повтор со
строгой инструкцией, затем шаблонный фолбэк.

**Шаблонный фолбэк:** H1 = `Title Case(primary_keyword)`, интро и секции собираются из
запросов кластера по фиксированным формулировкам, FAQ — из топ-запросов. Страница остаётся
структурно полной, но текст очевидно шаблонный — это и показывает вклад LLM.

---

## 12. `render/` — требования к странице

Общие: один файл на страницу, ноль внешних запросов (CSS инлайном в `<style>`, графика —
инлайн-SVG), относительные ссылки, открывается по `file://`.

Чеклист лендинга (`templates/landing.html`) — по нему же идёт приёмка:
- [ ] `<!doctype html>`, `<html lang="en">`, `<meta charset>`, `<meta name="viewport">`
- [ ] `<title>` = `PageContent.title`
- [ ] `<meta name="description">`, `<link rel="canonical">` (`config.site.base_url` + slug)
- [ ] `og:title`, `og:description`, `og:type`, `og:url`
- [ ] хлебные крошки `<nav aria-label="Breadcrumb">`: Home → Landing pages → страница
- [ ] ровно один `<h1>` = `PageContent.h1`, содержит `primary_keyword`
- [ ] вводный абзац
- [ ] 3–4 `<section>`, в каждой `<h2>`
- [ ] сетка ассетов: 8 инлайн-`<svg role="img">` с `<title>` и `aria-label`
- [ ] FAQ: `<h2>` + `<h3>` на вопрос
- [ ] `<nav>` «Related pages» — ссылки на все остальные страницы набора keyword-анкорами
- [ ] один `<script type="application/ld+json">` с `@graph`: `WebPage`, `FAQPage`, `BreadcrumbList`
- [ ] футер с пометкой: имя исходной выгрузки, дата генерации, режим (`llm` / `no-llm`)

`templates/index.html` — хаб: `<h1>`, таблица кластеров (primary keyword, число запросов,
clicks, impressions, avg position, potential, ссылка) и краткая сводка по исключённым.

---

## 13. `reporting/`

`out/excluded.csv`: `query,clicks,impressions,position,reason,note,source`.

`out/report.json`:
```json
{
  "generated_at": "ISO-8601",
  "input": {"path": "", "rows_read": 0, "queries": 0, "deduped": 0, "truncated": 0,
            "columns_found": [], "columns_missing": []},
  "mode": {"llm": true, "keep_brand": false, "keep_third_party": false,
           "models": {"classify": "", "cluster": "", "content": ""}},
  "filters": {"kept": 0, "dropped": 0, "by_reason": {"own_brand": 0}},
  "clusters": [{"slug": "", "primary_keyword": "", "intent": "", "file": "",
                "queries": [], "clicks": 0, "impressions": 0, "avg_position": 0.0,
                "potential": 0.0, "source": "", "content_source": ""}],
  "llm_usage": [{"stage": "", "model": "", "calls": 0, "cache_hits": 0,
                 "input_tokens": 0, "output_tokens": 0}],
  "warnings": []
}
```

`report.json` — единственный источник цифр для README: пункт «какие запросы отбросили и
почему» пересказывается из `filters.by_reason`, а не пишется вручную.

---

## 14. `llm/` — клиент

- Anthropic Messages API, structured output через tool use с JSON-схемой, `temperature=0`.
- ID моделей только из `config.models`, в коде не хардкодятся.
- Retry с экспоненциальным backoff на `429`, `5xx`, таймаутах: `config.llm.max_retries` (4).
- Кэш: `.cache/llm/<sha256(model + промпт + схема + payload)>.json`. Повторный прогон на тех
  же данных не тратит токены и даёт тот же результат. Отключается `--no-cache`.
- Промпты читаются из `config/prompts/*.md` — доработка формулировок не требует правки кода.
- Учёт токенов по стадиям пишется в `report.llm_usage`.

---

## 15. CLI (`cli/options.py`)

```
python run.py --input PATH [options]

  --input PATH              выгрузка .csv или .zip (обязательно)
  --out DIR                 каталог результата (по умолчанию out/)
  --config PATH             config/config.toml
  --target-pages INT        целевое число лендингов (10)
  --tolerance INT           допустимое отклонение (2)
  --max-queries INT         сколько запросов брать из выгрузки (500)
  --keep-brand              не исключать брендовые запросы
  --keep-third-party        не исключать чужие бренды и связанные символы
  --no-llm                  офлайн-режим: лексическая кластеризация + шаблоны
  --no-cache                игнорировать дисковый кэш LLM
  --log-level LEVEL         INFO по умолчанию
```

Приоритет значений: **флаг CLI > `config.toml` > встроенный дефолт**.

Коды выхода: `0` — успех; `1` — ошибка входных данных (файл не читается, нет колонки
запроса); `2` — после фильтров не осталось запросов для кластеризации.

Ключ API — только из переменной окружения `ANTHROPIC_API_KEY`; в конфиг и репозиторий не
попадает.

---

## 16. Деградация вместо падения

| ситуация | поведение |
|---|---|
| нет `ANTHROPIC_API_KEY` | предупреждение, офлайн-режим целиком |
| `--no-llm` | офлайн-режим, LLM не вызывается |
| ошибка API после всех повторов | предупреждение, офлайн-фолбэк для этой стадии |
| невалидный JSON от модели | один повтор со строгой инструкцией, затем фолбэк стадии |
| ответ модели неполный | пробелы заполняются детерминированно (§9, §10, §11) |
| выгрузка без колонок метрик | нули, ранжирование деградирует, предупреждение |
| выгрузка на 5000 строк | берётся топ-`--max-queries` по потенциалу, остальное `truncated` |

Любая деградация видна в `report.warnings` — молча подменять поведение нельзя.

---

## 17. Приёмка

1. `python run.py --input data/gsc_queries.csv --out out/` → `index.html`, ~10 лендингов,
   `report.json`, `excluded.csv`; код выхода 0.
2. `out/index.html` открывается по `file://`, внешних запросов ноль (devtools → Network),
   перелинковка работает в обе стороны.
3. `pytest` зелёный на фикстурах: BOM, разделитель `;`, русские заголовки, `54.95%`,
   `26,306`, перемешанный порядок колонок, отсутствие колонки CTR, 5000 строк, пустой файл.
4. `--no-llm` даёт полный набор страниц без ключа API.
5. Повторный прогон с LLM берёт всё из кэша (`llm_usage.cache_hits` > 0, токены 0).
6. По чеклисту §12: один H1, валидный JSON-LD, лимиты title/description, ни в одном
   заголовке нет брендовых и чужих запросов.
