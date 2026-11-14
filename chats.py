# -*- coding: utf-8 -*-
"""
Чат-контур «Честный Гараж» — агент вступает ПОСЛЕ диалога Бадри.
Работает после razbor.py в том же job GitHub Actions. Компьютер не нужен.

Логика (решение владельца 05.11):
  - Бадри отвечает первичные контакты и общается с клиентом. Пока Бадри не ответил —
    агент чат НЕ трогает (никаких черновиков).
  - Когда в чате есть ответы сервиса (Бадри пообщался), агент читает переписку
    и определяет: есть интерес или нет.
  - Интерес есть → агент вступает: принимает заявку в воронку, называет сделку,
    двигает этап, ставит задачи, и САМ пишет клиенту в чат Авито строго по шаблону
    (уточнить время приезда / запросить телефон). Телефон из чата — в контакт.
  - Интереса нет → принять и закрыть 143 с причиной (причина уходит в сводку
    для причёски объявлений).

ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
  - Агент не пишет клиенту ничего, кроме утверждённых шаблонов TEMPLATES ниже.
  - Если телефон клиент уже оставил — агент НЕ пишет, только двигает CRM.
  - Чаты без ответа сервиса пропускаются молча (их ведёт Бадри).
  - Только 9:00–20:00 мск (рабочее окно проверяется тут же; razbor.py дублирует).

Переменные окружения:
  AVITO_CLIENT_ID / AVITO_CLIENT_SECRET — приложение Авито (GitHub Secrets)
  AMO_TOKEN, MOONSHOT_API_KEY — общие с razbor.py
  CHAT_WINDOW_DAYS — глубина свежих чатов (по умолчанию 3)
  MAX_CHATS_PER_RUN — лимит заявок за прогон (по умолчанию 10)
  DRY_RUN — "true": только лог, без записи в amo и без отправки клиенту
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
AVITO_UID = 12626609

AMO_BASE = os.environ.get("AMO_BASE", "https://autoservice61.amocrm.ru").rstrip("/")
AMO_TOKEN = os.environ.get("AMO_TOKEN", "")
KIMI_BASE = os.environ.get("KIMI_BASE", "https://api.moonshot.ai").rstrip("/")
KIMI_KEY = os.environ.get("MOONSHOT_API_KEY", "")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
# NO_SEND=1 — CRM разбираем как обычно, но клиенту не пишем: вместо отправки
# шаблона ставим задачу человеку на 9:00 с текстом шаблона (ночные прогоны).
NO_SEND = os.environ.get("NO_SEND", "false").lower() == "true"
WINDOW_DAYS = int(os.environ.get("CHAT_WINDOW_DAYS", "3"))
MAX_PER_RUN = int(os.environ.get("MAX_CHATS_PER_RUN", "6"))

# люди и статусы (синхронно с razbor.py)
BADRI = 14367338
KONSTANTIN = 14221122
ST_FIRST = 88470414   # Первичный контакт
ST_TEPLY = 88470418   # Тёплые
ST_PRIEDET = 88470422 # приедет
ST_PRIEHAL = 88470426 # приехал
ST_CLOSED = 143       # Закрыто не реализовано
LOSS_NE_NASH_PROFIL = 24763846
TAG_CHAT = "Чат"  # тег сделок чат-контура Авито
TAG_CALL = "Звонок"  # тег сделок звонкового контура (razbor.py)
PHONE_FIELD = 1133163

# утверждённые шаблоны исходящих (только они, ничего от LLM клиенту)
T_WHEN = ("Здравствуйте! Подскажите, когда планируете подъехать? "
          "Подберём удобное время.")
T_PHONE = ("Отлично! Оставьте, пожалуйста, номер телефона — зафиксируем запись "
           "и перезвоним подтвердить.")
# шаги эскалации дожима (решение владельца 06.11): не чаще одного касания
# в сутки, привязка к словам клиента о приезде/переносе.
T_ACTUAL = ("Здравствуйте! Подскажите, актуален ещё вопрос? Работаем с 9:00 "
            "до 19:00, в субботу до 17:00 — подберём удобное время.")
T_SOFT = ("Здравствуйте! Если вопрос ещё актуален — напишите, подберём время "
          "и посчитаем точно по вашей машине.")
T_CONFIRM = ("Подскажите, к какому часу планируете? Работаем до 19:00 "
             "(в субботу до 17:00). Зафиксирую запись.")
T_NEWDATE = ("Хорошо, переносим без проблем. На какой день будет удобно? "
             "Подберём время.")
# финал: дожим проигнорирован ≥ суток → прощаемся и закрываем (владелец 06.11)
T_BYE = ("Здравствуйте! Видим, что вопрос, скорее всего, уже не актуален — "
         "закрываем обращение. Если передумаете — напишите, всегда подберём время.")
T_ADDRESS = ("Наш адрес: Фурмановская улица, 150Б. Работаем с 9:00 до 19:00, "
             "в субботу до 17:00.")
# F28: подтверждение записи накануне приезда (формулировка из регламента S4,
# утверждена владельцем 05.11: «Да → за день до приезда → "Завтра ждём вас.
# Всё получается приехать?"»)
T_CONFIRM_DAY = "Добрый день! Завтра ждём вас. Всё получается приехать?"
# F27: клиент сообщил, что УЖЕ приехал / ждёт на месте
ARRIVED_RE = re.compile(
    r"приехал(а)?\b|уже (у вас|здесь|жду)|жду (у входа|на парковке|у вас)|"
    r"на месте|подъехал(а)? (к вам|уже)|стою (у вас|у входа)", re.IGNORECASE)
# регулярка chat_id в заметках (движок дожима находит чат сделки по ней)
CHAT_ID_RE = re.compile(r"chat_id=(u2i-[\w~\-]+)")
# клиент «сам наберёт» → не дожимаем, напоминаем через 4 дня (схема владельца)
SAM_NABERET_RE = re.compile(
    r"подумаю|подумать|напишу (вам )?позже|сам (наберу|позвоню)|перезвоню|"
    r"свяжусь (с вами|позже)|свяжусь\b|связаться|наберу|отпишусь|"
    r"позвоню (вам )?(завтра|позже|сегодня|вечером|утром)|"
    r"напишу (вам )?(завтра|сегодня|вечером|позже)|на следующей неделе|"
    r"обращусь|не знаю когда|когда будет удобно|пока не планирую|пока не знаю",
    re.IGNORECASE)
# клиент закрывает вопрос сам → закрываем сделку без сообщений
CLOSE_RE = re.compile(
    r"не актуал|неактуал|уже не нужн|уже сделал|сделал в другом месте|"
    r"в другом месте|выбрал других|у других|передумал|не интересует|отказ|"
    r"не требуется|пока не нужн|не буду|отпад|отказал|хватит|отпишитесь|"
    r"не надо|не, спасибо|не стоит|отстаньте|"
    r"нашол\b|наш(е|ё)л(а)? (уже )?(друг|решени|где|вариант|мастер|сервис|специалист)"
    r"|сам разобрался|решил(а)? сам|сам(а)? починил|уже решил",
    re.IGNORECASE)
# финал переписки — отказ сервиса + вежливое подтверждение клиента = НЕ интерес
# (кейс Данил 06.11: «Таким не занимаемся» → «Понял» — контур кинул в тёплые и
# написал «когда приедете»; кейс Игорь 07.11: «В бокс не залезет» → клиент
# промолчал — контур создал сделку и собрался дожимать «актуален?»)
REFUSAL_RE = re.compile(
    r"не занимаемся|таким не|такое не делаем|не делаем|не сделаем|"
    r"не моем|не моим|не обслуживаем|не залезет|не влезет|не влез|"
    r"не пролезет|в бокс не|не поместится", re.IGNORECASE)
ACK_RE = re.compile(r"^(по?нял|ясно|спасибо|ок\b|окей|хорошо|ладно|понятно|"
                    r"хорош|благодарю|да нет|нет спасибо)", re.IGNORECASE)
# F17: короткое подтверждение без вопроса — НЕ «вопрос по существу» (кейсы
# Егор Пилипец/Евгений 11.11: на «Отлично» и «Принял, спасибо» движок
# пересоздавал задачу «ответить по существу» и блокировал себе дожим).
ACK_ANY_RE = re.compile(
    r"^(отлично|супер|здорово|принял|приняла|жду|до встречи|договорились|"
    r"хорошо|спасибо|благодарю|ок\b|окей|понял|поняла|ясно|понятно|ладно|"
    r"буду|согласен|согласна|высылайте|ждём)\b", re.IGNORECASE)
# ключи последней отправки, после которых тишина ≥24 ч → T_BYE и закрытие
BYE_KEYS = ("actual", "soft", "дожим дневного прогона", "дожим актуальности")
# шаговые ключи дожима — только они образуют лестницу (T_ACTUAL → T_SOFT → bye);
# прочие отправки (предложение осмотра и т.п.) лестницу не блокируют
FUP_KEYS = ("actual", "soft", "confirm", "bye", "newdate",
            "дожим дневного прогона", "дожим актуальности")
SENT_KEY_RE = re.compile(r"Отправлено клиенту \((.+?)\):")
# клиент называет день/время приезда → подтверждаем и фиксируем запись
SCHED_RE = re.compile(
    r"сегодня|завтра|послезавтра|суббот|воскресен|понедельник|вторник|среду|"
    # дата: требуем ДВУЗНАЧНЫЙ месяц (12.11, 3.12) — иначе «1.6»/«2.0»
    # (объём двигателя) матчились как «1 июня» и сделки улетали в
    # «Приедет» без договорённости (кейс Радик 14.11)
    r"четверг|пятницу|\b\d{1,2}[./]\d{2}\b|приеду|подъеду|заеду|привезу|"
    r"до обеда|утр|вечер|час(а|ов)?\b|к \d{1,2}[.:]?\d{0,2}|"
    r"в \d{1,2}[.:]?\d{0,2}\b", re.IGNORECASE)
# клиент «вот-вот приедет» → никаких дожимов, только задача «Приехал?»
# (кейс Никита-форсунки 07.11: «щас ищу кто прикурит», а контур слал
# «актуален?» каждый день — владелец: «ждёт пока прикурит, а мы долбим»)
PATIENT_RE = re.compile(
    r"прикур|выехал|уже еду|в пути|доезжаю|скоро буду|через \d+ минут|"
    r"сейчас подъеду|сейчас приеду|позже приеду|подъеду позже|минут через|"
    r"по пути|собираюсь выезжать", re.IGNORECASE)
# отметка человека «не приехал» → ветка переноса/закрытия, не дожим
# (кейс Nikolay 10.11: «Не приехал» в заметке не вело никуда, сделка висела)
NO_SHOW_RE = re.compile(
    r"не приехал|не приш[её]л|не дождались", re.IGNORECASE)
# только приветствие от сервиса → интерес не «разобран», отвечает Бадри
# (кейс Кирилл/редуктор 10.11: сервис написал только «Здравствуйте», а
# сделка ушла в «Тёплые» вопросом, на который ещё не ответили)
GREETING_RE = re.compile(
    r"^\s*(здравствуйте|добрый (день|вечер|утро)|привет)\.?!?\s*$",
    re.IGNORECASE)
# VIN: 17 латинских букв/цифр подряд (клиенты часто пишут с пробелами — сжимаем)
VIN_RUN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
def has_vin(text):
    compact = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    return bool(VIN_RUN_RE.search(compact))
# клиент спрашивает цену/стоимость — перед шаблонами проверяем, что на вопрос
# уже ответили по существу (кейс Евгений/Даниил 06.11: ушёл «оставьте номер»,
# хотя клиент ждал цену)
PRICE_ASK_RE = re.compile(
    r"сколько стоит|сколько будет|во сколько|стоимост|цена|цену|цене|почём|"
    r"почем|дешевле|дорого|прайс|за сколько|на сколько", re.IGNORECASE)
def price_answered(text):
    # ответ по существу = есть сумма (трёхзначное+ число) в ответе сервиса
    return bool(re.search(r"\d[\d\s]{2,}\s*(руб|₽|т\.?р|тыс|тысяч)?", text or "")
                and re.search(r"\d{3,}", text or ""))
FUP_SILENCE_FIRST = 20   # ч тишины после нашего ответа → первый дожим
FUP_GAP = 20             # минимум ч между любыми отправками клиенту
LOSS_PROPALA = 24393498  # Пропала потребность
LOSS_DRUGIE = 24393506   # Выбрали других
LOSS_DUBL = 24765102     # Дубль

SYSTEM = """Ты — оператор сделок автосервиса «Честный Гараж». По переписке чата Авито определи, есть ли у клиента интерес к услуге, и верни СТРОГО один JSON-объект.

ИНТЕРЕС ЕСТЬ, если клиент обсуждает выполнение работы (уточняет цену/сроки/запчасти/приезд) и НЕ отказался. «Дорого, но не отказался», «подумаю», «приеду» — это интерес.
ИНТЕРЕСА НЕТ, если: клиент отказался; «не делаем такое» (тюнинг, чип и пр. — не наш профиль); клиент НЕ ОТВЕЧАЕТ ДАВНО (больше суток) после того, как получил цену/ответ, и явно ушёл; просьба невозможна. НЕЛЬЗЯ закрывать «пропал после цены», если после нашего ответа прошло мало времени — это тишина, не отказ. ВАЖНО: если ПОСЛЕДНЯЯ реплика СЕРВИСА — отказ («не занимаемся», «таким не занимаемся», «не делаем», «полную не делаем»), а клиент лишь вежливо подтвердил («понял», «спасибо», «ок») — интереса НЕТ, даже если вопрос в начале звучал по делу. Смотри на ФИНАЛ переписки, а не на первый вопрос.

ПОЛЯ:
- interest: true/false
- reason: краткая причина (для закрытия/сводки), если интереса нет; иначе null
- car: марка модель год, если ясно, иначе null
- request: суть запроса 5–10 слов
- client_has_phone: true, если клиент сам оставлял телефон в чате; иначе false
- phone: номер телефона из чата (формат 89xxxxxxxxx или +7…), если есть; иначе null
- said_will_come: true, если клиент сказал, что приедет (даже без даты); иначе false
- next_step: одно из "when" (интерес есть, время приезда неясно → уточнить когда приедет), "phone" (готов приехать → запросить телефон), "none" (писать не надо)
- summary: одна фраза итога для внутренней заметки

ВАЖНО ПРО next_step: "when"/"phone" ставь ТОЛЬКО если клиенту уже ответили по существу его последнего вопроса. Если клиент ждёт цену/расчёт/ответа по сути (особенно его последняя реплика — вопрос «сколько стоит/стоимость?», а сервис на него не ответил) — ставь "none": шаблон про телефон/время перед ответом на вопрос отправлять нельзя.

Формат: {"interest": true, "reason": null, "car": "…", "request": "…", "client_has_phone": false, "phone": null, "said_will_come": false, "next_step": "when", "summary": "…"}"""


def log(msg):
    print(f"[{datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}] {msg}", flush=True)


def http(method, url, token=None, body=None, tries=3, timeout=45, form=False):
    if form:
        data = body.encode("utf-8") if isinstance(body, str) else None
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        if form:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "chg-chats/1.0")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                last = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
            except Exception:
                last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")


def avito_token():
    r = http("POST", "https://api.avito.ru/token", form=True, body=(
        f"grant_type=client_credentials"
        f"&client_id={os.environ['AVITO_CLIENT_ID']}"
        f"&client_secret={os.environ['AVITO_CLIENT_SECRET']}"))
    return r["access_token"]


def amo_get(path):
    return http("GET", f"{AMO_BASE}{path}", token=AMO_TOKEN)


def amo(method, path, body):
    return http(method, f"{AMO_BASE}{path}", token=AMO_TOKEN, body=body)


def embedded(resp, key):
    return (resp.get("_embedded") or {}).get(key) or []


def msg_text(m):
    return ((m.get("content") or {}).get("text") or f"[{m.get('type') or 'файл'}]").strip()


def classify(transcript, client_name, item_title):
    user = (f"Объявление: «{item_title}»\nКлиент: {client_name}\n\nПЕРЕПИСКА:\n"
            + transcript[:6000])
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    last_err = None
    for attempt in range(3):
        body = {
            "model": KIMI_MODEL,
            "temperature": float(os.environ.get("KIMI_TEMPERATURE", "1")),
            "response_format": {"type": "json_object"},
            "messages": msgs,
        }
        try:
            resp = http("POST", f"{KIMI_BASE}/v1/chat/completions", token=KIMI_KEY,
                        body=body, tries=2, timeout=120)
            content = (resp["choices"][0]["message"]["content"] or "").strip()
            content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
            return json.loads(content)
        except (json.JSONDecodeError, KeyError, RuntimeError) as e:
            last_err = e
            log(f"    classify: попытка {attempt + 1} не удалась ({e})")
            msgs = msgs + [{"role": "user", "content":
                            "Верни СТРОГО один JSON-объект, без текста до и после."}]
            time.sleep(3)
    raise RuntimeError(f"LLM не вернул JSON: {last_err}")


def today_18_msk():
    d = datetime.now(MSK).date()
    return int(datetime(d.year, d.month, d.day, 18, 0, tzinfo=MSK).timestamp())


def tomorrow_10_msk():
    d = datetime.now(MSK).date() + timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, 10, 0, tzinfo=MSK).timestamp())


def next_9_msk():
    """Ближайшие 9:00 мск: сегодня, если ещё не наступили, иначе завтра."""
    now = datetime.now(MSK)
    d = now.date()
    ts = datetime(d.year, d.month, d.day, 9, 0, tzinfo=MSK)
    if now >= ts:
        ts += timedelta(days=1)
    return int(ts.timestamp())


# F31 (решение владельца 12.11): дожимы уходят ОДНОЙ утренней пачкой к началу
# рабочего дня (окно 8:00–9:30 мск), а не хаотично по тикам весь день.
# Обработка (приём, задачи, этапы) работает весь день — окно только на
# отправку шаблонов из followup_engine/send_pending_templates.
DAILY_SEND_START = os.environ.get("DAILY_SEND_START", "8:00")
DAILY_SEND_END = os.environ.get("DAILY_SEND_END", "9:30")


def in_daily_send_window(now=None):
    def _hm(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    cur = (now or datetime.now(MSK)).hour * 60 + (now or datetime.now(MSK)).minute
    return _hm(DAILY_SEND_START) <= cur <= _hm(DAILY_SEND_END)


def norm_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


def adopt_integration_leads(tok, by_name, now):
    """F23 (кейс «Замена амортизаторов»/Сергей 11.11): интеграция Авито→amo
    иногда принимает чат НАПРЯМУЮ в воронку (created_by=0), минуя лоток —
    такая сделка без тега «Чат» и без chat_id= в заметках невидима для дожима
    и висит мёртвым грузом. Находим открытые сделки без меток контура,
    сопоставляем с чатом по имени контакта (+близость времени), ставим
    тег «Чат» и заметку с chat_id= — дальше сделку ведёт followup_engine."""
    from_ts = int((now - timedelta(days=3)).timestamp())
    try:
        resp = amo_get(f"/api/v4/leads?filter[pipeline_id][]=11282498"
                       f"&filter[updated_at][from]={from_ts}&limit=250")
    except Exception as e:
        log(f"усыновление: не удалось получить сделки ({e})")
        return 0
    adopted = 0
    for l in embedded(resp, "leads"):
        if l.get("status_id") in (ST_CLOSED, 142):
            continue
        tags = [t.get("name") for t in (l.get("_embedded") or {}).get("tags", [])]
        if TAG_CHAT in tags or TAG_CALL in tags:
            continue
        # F23-огрех 12.11: оболочки телефонии («Исходящий/Входящий/Пропущенный
        # звонок …») усыновились в чат-контур с чужим chat_id до того, как
        # razbor закрыл их как дубли. Усыновляем только чат-подобные сделки.
        if re.match(r"^\s*(исходящий|входящий|пропущенный) звонок", (l.get("name") or "").lower()):
            continue
        lid = l["id"]
        try:
            notes = embedded(amo_get(f"/api/v4/leads/{lid}/notes?limit=100"), "notes")
            if any(CHAT_ID_RE.search(((n.get("params") or {}).get("text") or ""))
                   for n in notes):
                continue
            full = amo_get(f"/api/v4/leads/{lid}?with=contacts")
            names = []
            for c in embedded(full, "contacts"):
                nm = (c.get("name") or "").strip().lower()
                # имя контакта, не похожее на телефон/системное
                if nm and not re.match(r"^[+\d\s()\-]+$", nm):
                    names.append(nm)
            best, best_d = None, 1e18
            for nm in names:
                for c in by_name.get(nm, []):
                    d = abs((c.get("created") or 0) - (l.get("created_at") or 0))
                    if d < best_d:
                        best, best_d = c, d
            if not best:
                # имени нет (контакт пустой, кейс «Замена амортизаторов») —
                # допускаем чисто временной подбор, но с жёстким окном
                for c in by_name.get("*", []):
                    d = abs((c.get("created") or 0) - (l.get("created_at") or 0))
                    if d < best_d:
                        best, best_d = c, d
            limit = 6 * 3600 if names else 2 * 3600
            if not best or best_d > limit:
                continue
            cid = best["id"]
            amo("PATCH", "/api/v4/leads", [{"id": lid,
                 "_embedded": {"tags": [{"name": TAG_CHAT}]}}])
            amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                "text": f"Сделка принята интеграцией Авито напрямую (F23, {now.strftime('%d.%m.%Y %H:%M')}) — "
                        f"подхвачена в чат-контур: chat_id={cid}. Клиент: {names[0] if names else '?'}. "
                        f"Дальше — стандартный дожим по схеме."}}])
            adopted += 1
            log(f"  усыновление: сделка {lid} «{(l.get('name') or '')[:40]}» → chat {cid[:10]}… (Δ{int(best_d)}с)")
        except Exception as e:
            log(f"  усыновление {lid}: ошибка ({e})")
    if adopted:
        log(f"усыновление: подхвачено {adopted} интеграционных сделок")
    return adopted


def find_chat_for_item(item, by_name):
    """Подбор чата Авито для заявки лотка.

    F10 (11.11): раньше искали только по users[0].name — но клиент может идти
    НЕ первым в users (кейс Дмитрий(Рябуха): users[0] — наш профиль, клиент —
    users[1], чат не находился, заявка голодала в лотке). Теперь: точное имя
    → любое имо в users → fallback по близости времени среди всех свежих чатов.
    """
    md = item.get("metadata") or {}
    name = (md.get("from") or "").strip().lower()
    got = md.get("received_at") or 0
    last = md.get("last_message_text") or 0
    if isinstance(last, str):
        last = 0  # в метаданных бывает текст, а не таймстамп (затык 11.11)

    def dist(c):
        d = abs((c.get("created") or 0) - got)
        if last:
            d = min(d, abs((c.get("updated") or 0) - last))
        return d

    best, best_d = None, 1e18
    for c in by_name.get(name, []):
        d = dist(c)
        if d < best_d:
            best, best_d = c, d
    if best_d <= 120:
        return best, best_d
    # имя не совпало ни с одним чатом — ищем ближайший по времени среди всех
    for c in by_name.get("*", []):
        d = dist(c)
        if d < best_d:
            best, best_d = c, d
    return (best, best_d) if best_d <= 3600 else (None, best_d)


PHONE_SCAN_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")


def scan_phone(msgs):
    """Детерминированный поиск телефона КЛИЕНТА во входящих (F18): LLM иногда
    теряет номер, а владелец требует 'оставил номер — занеси в карточку'
    (кейсы Иван/whtevr 11.11). Возвращает +7XXXXXXXXXX или None."""
    for m in msgs:
        if m.get("direction") != "in":
            continue
        mm = PHONE_SCAN_RE.search(msg_text(m) or "")
        if mm:
            ph = norm_phone(mm.group(0))
            if ph:
                return ph
    return None


def ensure_contact_phone(lid, lead_name, phone):
    """Телефон из чата — в контакт сделки (создать контакт при отсутствии,
    привязка через /link — _embedded при создании сделку не привязывает)."""
    try:
        contacts = embedded(amo_get(f"/api/v4/leads/{lid}?with=contacts"), "contacts")
        if not contacts:
            created = amo("POST", "/api/v4/contacts", [{
                "name": (lead_name or f"Клиент {phone}")[:100],
                "custom_fields_values": [{"field_id": PHONE_FIELD,
                                          "values": [{"value": phone}]}]}])
            cid2 = ((created.get("_embedded") or {}).get("contacts") or [{}])[0].get("id")
            if cid2:
                amo("POST", f"/api/v4/leads/{lid}/link",
                    [{"to_entity_id": cid2, "to_entity_type": "contacts"}])
            return True
        patched = False
        for c in contacts:
            try:
                detail = amo_get(f"/api/v4/contacts/{c['id']}")
                vals = detail.get("custom_fields_values") or []
                has = any(v.get("field_id") == PHONE_FIELD and v.get("values")
                          for v in vals)
            except Exception:
                has = True
            if not has:
                amo("PATCH", "/api/v4/contacts", [{"id": c["id"],
                     "custom_fields_values": [{"field_id": PHONE_FIELD,
                                               "values": [{"value": phone}]}]}])
                patched = True
        return patched
    except Exception:
        return False


# F33: просьба об отзыве — шаблон утверждён владельцем 13.11
T_REVIEW = ("Здравствуйте! Спасибо, что доверили нам автомобиль. Если визит понравился — "
            "оставьте, пожалуйста, отзыв на нашем объявлении, это очень помогает. Всего доброго!")

# правило владельца 11.11: клиент сообщает, что УЖЕ БЫЛ у нас → просим отзыв
CLIENT_WAS_RE = re.compile(
    r"был(а)? (у вас|в сервисе|к вам)|приезжал(а)? (к вам|к нам)|"
    r"сделал(а)? (у вас|тут|здесь)|делал(а)? у вас|обслуживал(ся|ась) у вас|"
    r"сделали у вас|пользовал(ся|ась) вашими услугами|делали (у вас|тут)",
    re.IGNORECASE)



def ask_reviews(tok, now):
    """F33 (решение владельца 13.11): сделки «Успешно реализовано» (142) с
    чатом Авито получают просьбу оставить отзыв — один раз, по маркеру-заметке.
    Звонковые успехи сюда не попадают (нет chat_id) — там просьбу озвучивает
    человек. Работает в рабочем окне прогона (9:00–21:00)."""
    try:
        resp = amo_get("/api/v4/leads?filter[pipeline_id][]=11282498"
                       "&filter[statuses][]=142&limit=250")
    except Exception as e:
        log(f"отзывы: не удалось получить сделки ({e})")
        return 0
    wanted = {142}
    sent_n = 0
    for l in embedded(resp, "leads"):
        if l.get("status_id") not in wanted:
            continue  # фильтр статусов у токена игнорируется — перепроверяем
        lid = l["id"]
        try:
            notes = embedded(amo_get(f"/api/v4/leads/{lid}/notes?limit=100"), "notes")
            if any("Отзыв запрошен" in (((n.get("params") or {}).get("text") or ""))
                   for n in notes):
                continue
            cid = None
            for n in notes:
                m = CHAT_ID_RE.search(((n.get("params") or {}).get("text") or ""))
                if m:
                    cid = m.group(1)
            if not cid:
                continue
            # F33-страховка (указание владельца 13.11): отзыв просим ТОЛЬКО у
            # реально обслуженных. Статус 142 мог поставиться «вслепую», поэтому
            # требуем след визита: либо в карточке (заметки/задачи про приезд,
            # F27), либо в переписке («приехал/уже у вас/жду/на месте»).
            hist = " ".join(note_text(n) for n in notes).lower()
            evidence = bool(re.search(
                r"приехал|уже (у вас|жду)|на месте|подъехал|стою у|"
                r"переведена? в «приехал»|приехал\?", hist))
            if not evidence:
                try:
                    msgs = http("GET", f"https://api.avito.ru/messenger/v3/accounts/"
                                       f"{AVITO_UID}/chats/{cid}/messages/", token=tok).get("messages", [])
                    in_txt = " ".join(msg_text(m) for m in msgs
                                      if m.get("direction") == "in")
                    evidence = bool(ARRIVED_RE.search(in_txt))
                except Exception:
                    evidence = False
            if not evidence:
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": "Отзыв НЕ запрошен (F33-страховка): подтверждения визита в чате "
                            "и карточке не найдено — сделка в 142, но клиент, возможно, не "
                            "ремонтировался. Проверить перед ручной просьбой об отзыве."}}])
                log(f"  отзыв: сделка {lid} — пропуск, нет подтверждения визита")
                continue
            http("POST", f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}"
                         f"/chats/{cid}/messages", token=tok,
                 body={"message": {"text": T_REVIEW}, "type": "text"})
            amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                "text": f"Отправлено клиенту (review): просьба оставить отзыв на Авито "
                        f"({now.strftime('%d.%m.%Y')}, F33)."}}])
            sent_n += 1
            log(f"  отзыв: сделка {lid} «{(l.get('name') or '')[:40]}» — просьба отправлена")
        except Exception as e:
            log(f"  отзыв {lid}: ошибка ({e})")
    if sent_n:
        log(f"отзывы: отправлено просьб {sent_n}")
    return sent_n


def send_pending_templates(tok):
    """Дневной дожим: ночные прогоны (NO_SEND) не пишут клиентам, а оставляют
    в заметке сделки 'chat_id=…: «шаблон»'. Здесь находим такие сделки и шлём.
    Работает в конце каждого дневного прогона; повторной отправки не будет —
    после отправки появляется заметка «Отправлено клиенту»."""
    from_ts = int((datetime.now(MSK) - timedelta(days=4)).timestamp())
    try:
        leads = http("GET",
                     f"{AMO_BASE}/api/v4/leads?filter[pipeline_id][]=11282498"
                     f"&filter[statuses][]={ST_FIRST}&filter[statuses][]={ST_TEPLY}&filter[statuses][]={ST_PRIEDET}"
                     f"&filter[updated_at][from]={from_ts}&limit=250", token=AMO_TOKEN)
    except Exception as e:
        log(f"дожим: не удалось получить сделки ({e})")
        return 0
    sent_n = 0
    pat = re.compile(r"chat_id=(\S+?): «(.+?)»")
    # фильтр статусов в amo у этого токена игнорируется — отбираем сами,
    # иначе дожим ходит по закрытым сделкам и шлёт клиентам лишнее
    wanted = {ST_FIRST, ST_TEPLY, ST_PRIEDET}
    for l in [x for x in embedded(leads, "leads") if x.get("status_id") in wanted]:
        tags = [t.get("name") for t in (l.get("_embedded") or {}).get("tags", [])]
        if TAG_CHAT not in tags:
            continue
        try:
            notes = embedded(amo_get(f"/api/v4/leads/{l['id']}/notes?limit=100"), "notes")
        except Exception:
            continue
        pending = sent = None
        for n in sorted(notes, key=lambda x: x.get("created_at", 0)):
            t = ((n.get("params") or {}).get("text") or "")
            if "chat_id=" in t and "НЕ отправлен" in t:
                m = pat.search(t)
                if m:
                    pending = m.groups()
            if "Отправлено клиенту" in t:
                sent = True
        if not pending or sent:
            continue
        cid, tpl = pending
        try:
            http("POST",
                 f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}/chats/{cid}/messages",
                 token=tok, body={"message": {"text": tpl}, "type": "text"})
            amo("POST", f"/api/v4/leads/{l['id']}/notes", [{"note_type": "common", "params": {
                "text": f"Отправлено клиенту (дожим дневного прогона): «{tpl}»"}}])
            sent_n += 1
            log(f"  дожим: сделка {l['id']}, chat {cid[:10]}…, шаблон отправлен")
        except Exception as e:
            log(f"  дожим: сделка {l['id']}: ошибка отправки ({e})")
    return sent_n


def followup_engine(tok):
    """Регулярный дожим тёплых чат-сделок (схема владельца 06.11).

    Для каждой открытой сделки с тегом «Чат» находим её чат Авито по заметке
    chat_id=… и смотрим ПОСЛЕДНИЕ сообщения (не стартовый вопрос — это баг
    кейса Дмитрия АКПП, где финал «Понял, спасибо» = отказ):
      1. клиент ответил, мы нет (тишина >2 ч):
         - «подумаю / сам наберу / на следующей неделе» → НЕ дожимаем,
           ставим напоминание через 4 дня со сценарием возврата;
         - называет день/время → шлём T_CONFIRM, сделку в «Приедет»,
           задача за день до приезда «всё получается?»;
         - вопрос по сути — ставим задачу «Ответить в чате» (человеку),
           сами не пишем (переписку пока ведёт сервис).
      2. мы ответили последними, клиент молчит:
         - 20–48 ч → T_ACTUAL (не чаще 1 касания в сутки);
         - ещё 48 ч → T_SOFT; дальше (>96 ч, 2+ касания) → задача
           «дожать вручную» Константину, сам больше не пишем.
      3. клиент просит перенос → T_NEWDATE и ждём новую дату.
    Защита от спама: между любыми отправками ≥ FUP_GAP ч, всего ≤2 дожима.
    """
    now = datetime.now(MSK)
    from_ts = int((now - timedelta(days=14)).timestamp())
    leads = amo_get(f"/api/v4/leads?filter[pipeline_id][]=11282498"
                    f"&filter[statuses][]={ST_FIRST}&filter[statuses][]={ST_TEPLY}"
                    f"&filter[statuses][]={ST_PRIEDET}"
                    f"&filter[updated_at][from]={from_ts}&limit=250&with=tasks")
    # фильтр статусов в amo у этого токена игнорируется — отбираем сами,
    # иначе движок дожима лезет в закрытые сделки и пишет их клиентам
    wanted = {ST_FIRST, ST_TEPLY, ST_PRIEDET}
    lead_rows_all = [l for l in embedded(leads, "leads") if l.get("status_id") in wanted]
    done = {"confirm": 0, "actual": 0, "soft": 0, "newdate": 0, "bye": 0,
            "closed": 0, "dup": 0, "refusal": 0,
            "sam_naberet": 0, "human": 0, "manual": 0, "skip": 0}

    def send(lead_id, cid, tpl, key):
        if not in_daily_send_window(now):
            # F31: дожим отложен до утреннего окна 8:00–9:30 — не шлём
            # хаотично в течение дня (решение владельца 12.11)
            log(f"  дожим+ {lead_id}: «{key}» отложен до утреннего окна {DAILY_SEND_START}–{DAILY_SEND_END}")
            return False
        http("POST", f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}"
                     f"/chats/{cid}/messages", token=tok,
             body={"message": {"text": tpl}, "type": "text"})
        # F30 (кейс Сергей/«Замена амортизаторов» 12.11): дожим ушёл —
        # висящую контрольную задачу логично перенести на завтра, иначе
        # отправили сообщение, а старая (часто уже просроченная) задача висит.
        try:
            till = tomorrow_10_msk()
            for t in open_tasks_by_id(lead_id):
                if (t.get("complete_till") or 0) < till:
                    amo("PATCH", "/api/v4/tasks", [{"id": t["id"], "complete_till": till}])
        except Exception:
            pass
        # прогноз следующего шага — чтобы в карточке было видно, что будет
        # дальше без вопроса «какой следующий шаг?» (просьба владельца 07.11)
        nxt = ""
        if key == "actual":
            bye_t = now + timedelta(hours=24)
            nxt = (f"\nСледующий шаг по схеме: молчит до "
                   f"{bye_t.strftime('%d.%m %H:%M')} → прощание «видим, что вопрос "
                   f"не актуален» + закрытие «Пропала потребность»; ответил → "
                   f"диалог по сценарию (дата → «Приедет»).")
        elif key == "soft":
            nxt = ("\nСледующий шаг по схеме: молчит сутки → прощание + закрытие "
                   "«Пропала потребность».")
        elif key == "confirm":
            nxt = ("\nСледующий шаг по схеме: назвал точное время → за день до "
                   "приезда задача «всё получается?»; по факту приезда → «Приехал».")
        elif key == "newdate":
            nxt = "\nСледующий шаг по схеме: ждём новую дату от клиента → «Приедет»."
        elif key == "bye":
            nxt = "\nСледующий шаг: сделка закрывается «Пропала потребность»."
        amo("POST", f"/api/v4/leads/{lead_id}/notes",
            [{"note_type": "common", "params": {
                "text": f"Отправлено клиенту ({key}): «{tpl}»{nxt}"}}])
        done[key] = done.get(key, 0) + 1
        log(f"  дожим+ {lead_id}: {key} → chat {cid[:10]}…")

    def close_lead(lead_id, loss_id, why):
        amo("PATCH", "/api/v4/leads", [{"id": lead_id, "status_id": ST_CLOSED,
                                       "loss_reason_id": loss_id}])
        amo("POST", f"/api/v4/leads/{lead_id}/notes",
            [{"note_type": "common", "params": {"text": why}}])
        mine = open_tasks_by_id(lead_id)
        if mine:
            amo("PATCH", "/api/v4/tasks",
                [{"id": t["id"], "is_completed": True} for t in mine])
            _task_cache.pop(lead_id, None)
        done["closed"] += 1

    _task_cache = {}
    def open_tasks_by_id(lead_id):
        """Открытые задачи сделки ПО API (amo не встраивает tasks в lead)."""
        if lead_id not in _task_cache:
            _task_cache[lead_id] = embedded(amo_get(
                f"/api/v4/tasks?filter[entity_id]={lead_id}"
                f"&filter[entity_type][]=leads&filter[is_completed]=0&limit=100"),
                "tasks")
        return _task_cache[lead_id]

    def open_tasks(l):
        return [t for t in embedded(l, "tasks") if not t.get("is_completed")]

    def has_manual_task(tasks):
        # «любая открытая задача» не считается: устаревшая «Когда приедет?»
        # иначе навсегда блокирует ветку ручного дожима (кейс Nikolay 10.11)
        return any("дожать" in (t.get("text") or "").lower()
                   or "вручную" in (t.get("text") or "").lower()
                   for t in tasks)

    def add_task(lead_id, text, till_ts, responsible=KONSTANTIN):
        _task_cache.pop(lead_id, None)  # кеш открытых задач протух
        _raw_add_task(lead_id, text, till_ts, responsible)
        # верификация (инцидент 14.11 «Перевозка умерших»: шаблон ушёл, а
        # задача не создалась — amo отвалился на 401 между двумя запросами):
        # перечитываем открытые; нет — повторяем; снова нет — флаг в карточку.
        try:
            ok = any(text[:40] in (t.get("text") or "") for t in open_tasks_by_id(lead_id))
            if not ok:
                time.sleep(8)
                _task_cache.pop(lead_id, None)
                _raw_add_task(lead_id, text, till_ts, responsible)
                _task_cache.pop(lead_id, None)
                ok = any(text[:40] in (t.get("text") or "") for t in open_tasks_by_id(lead_id))
            if not ok:
                amo("POST", f"/api/v4/leads/{lead_id}/notes", [{"note_type": "common", "params": {
                    "text": "ВНИМАНИЕ: контрольная задача НЕ создана (API amo дважды не подтвердил): "
                            f"«{text[:120]}». Создать вручную."}}])
                log(f"  дожим+ {lead_id}: ЗАДАЧА НЕ СОЗДАНА (флаг в карточке)")
        except Exception as e:
            log(f"  дожим+ {lead_id}: верификация задачи не удалась ({e})")

    def _raw_add_task(lead_id, text, till_ts, responsible=KONSTANTIN):
        # Правило владельца 06.11 (доуточнено 11.11): ОДНА открытая Kimi-задача
        # на сделку. Новая контрольная закрывает прежние — иначе копились пары
        # «Когда приедет?» + «за день до приезда подтвердить» (кейс 11.11:
        # на 4 сделках висело по 2 задачи, очередь раздута вдвое).
        mine = [t for t in open_tasks_by_id(lead_id)
                if (t.get("text") or "").startswith("Kimi для")]
        if mine:
            amo("PATCH", "/api/v4/tasks",
                [{"id": t["id"], "is_completed": True} for t in mine])
        amo("POST", "/api/v4/tasks", [{
            "text": text, "responsible_user_id": responsible,
            "complete_till": int(till_ts), "entity_id": lead_id,
            "entity_type": "leads"}])

    # --- межпрогонный антидубль: одна сделка на один чат Авито ---
    # (кейс Данил 06.11: Авито продублировал заявку, контур создал вторую
    # сделку на следующем прогоне — внутрипрогонный антидубль не сработал)
    lead_rows = []
    for l in lead_rows_all:
        tags = [t.get("name") for t in (l.get("_embedded") or {}).get("tags", [])]
        if TAG_CHAT not in tags:
            continue
        lead_rows.append(l)
    by_cid = {}
    for l in lead_rows:
        try:
            notes0 = embedded(amo_get(f"/api/v4/leads/{l['id']}/notes?limit=100"),
                              "notes")
        except Exception:
            continue
        cid0 = None
        for n in sorted(notes0, key=lambda x: x.get("created_at") or 0):
            m = CHAT_ID_RE.search(((n.get("params") or {}).get("text") or ""))
            if m:
                cid0 = m.group(1)
        if cid0:
            by_cid.setdefault(cid0, []).append((l, notes0))
    for cid0, rows in by_cid.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r[0].get("created_at") or 0)
        for l, _notes in rows[1:]:
            close_lead(l["id"], LOSS_DUBL,
                       f"Межпрогонный дубль: по чату {cid0} уже есть сделка "
                       f"{rows[0][0]['id']} («{rows[0][0].get('name', '')[:50]}»). "
                       f"Авито продублировал заявку — закрыто автоматически.")
            done["dup"] += 1
            lead_rows = [r for r in lead_rows if r["id"] != l["id"]]

    for l in lead_rows:
        lid = l["id"]
        try:
            notes = embedded(amo_get(f"/api/v4/leads/{lid}/notes?limit=100"),
                             "notes")
        except Exception:
            continue
        cid, last_sent_ts, n_sent, last_key = None, 0, 0, None
        sam_due, no_show = None, False
        fup_ts = []
        for n in sorted(notes, key=lambda x: x.get("created_at") or 0):
            t = ((n.get("params") or {}).get("text") or "")
            m = CHAT_ID_RE.search(t)
            if m:
                cid = m.group(1)
            if "Отправлено клиенту" in t:
                # лестница дожима считается только по шаговым ключам; разовые
                # сообщения (напр. «предложение осмотра», кейс Антон 07.11)
                # антиспам-зазор держат (last_sent_ts), но лестницу не блокируют
                last_sent_ts = max(last_sent_ts, n.get("created_at") or 0)
                km = SENT_KEY_RE.search(t)
                key = km.group(1) if km else ""
                if any(k in key for k in FUP_KEYS):
                    n_sent += 1
                    last_key = key
                    fup_ts.append(n.get("created_at") or 0)
            if "сам наберёт — вернуться" in t:
                mm = re.search(r"после (\d+)", t)
                if mm:
                    sam_due = int(mm.group(1))
            if NO_SHOW_RE.search(t):
                no_show = True
        if not cid:
            done["skip"] += 1
            continue
        try:
            msgs = http("GET", f"https://api.avito.ru/messenger/v3/accounts/"
                               f"{AVITO_UID}/chats/{cid}/messages/?limit=50",
                        token=tok).get("messages") or []
        except Exception as e:
            log(f"  дожим+ {lid}: не прочитан чат ({e})")
            continue
        ins = [m for m in msgs if m.get("direction") == "in"
               and not msg_text(m).startswith("[Системное")]
        outs = [m for m in msgs if m.get("direction") == "out"]
        if not outs:  # Бадри ещё не отвечал — агент молчит (жёсткое правило)
            continue
        # F35 (кейс Денис 14.11): закрытые сделки (142/143) НИКОГДА не
        # возвращаем в воронку по активности чата. Новое сообщение клиента
        # после закрытия = заметка + задача человеку (ответ на отзыв/вопрос),
        # статус не меняем.
        if l.get("status_id") in (142, ST_CLOSED):
            recent_in = [m for m in ins if (m.get("created") or 0) > (l.get("updated_at") or 0) - 2 * 86400]
            if recent_in and not any("после закрытия" in (((n.get("params") or {}).get("text") or "")) for n in notes):
                last_txt = msg_text(recent_in[0])[:100]
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"F35: клиент написал ПОСЛЕ ЗАКРЫТИЯ сделки: «{last_txt}» — статус не меняем. "
                            f"Если это ответ на просьбу об отзыве — поблагодарить; иначе разобрать вручную."}}])
                add_task(lid, "Kimi для Бадри: клиент написал после закрытия сделки "
                         f"({(l.get('name') or 'сделка')[:50]}): «{last_txt[:60]}» — ответить/поблагодарить, "
                         "статус НЕ открывать", BADRI, nowts + 4 * 3600)
                done["human"] += 1
            continue
        # F18: телефон клиента из чата — детерминированно в контакт (не ждём LLM)
        ph = scan_phone(msgs)
        if ph and not any("Телефон из чата" in ((n.get("params") or {}).get("text") or "")
                          for n in notes):
            if ensure_contact_phone(lid, l.get("name") or "", ph):
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common",
                    "params": {"text": f"Телефон из чата: {ph} (детерминированный "
                                       "скан входящих — F18)"}}])
                log(f"  дожим+ {lid}: телефон из чата {ph} — в контакт")
        # F19 (правило владельца 11.11): клиент пишет, что УЖЕ БЫЛ у нас →
        # поблагодарить и попросить отзыв. Срабатывает один раз (маркер-заметка).
        if l.get("status_id") in (ST_PRIEHAL, 142) and ins:
            if not any("отзыв" in (((n.get("params") or {}).get("text") or "").lower())
                       for n in notes):
                joined_recent = " ".join(msg_text(m) for m in ins[:6])
                if CLIENT_WAS_RE.search(joined_recent):
                    add_task(lid,
                             "Kimi для Бадри: клиент сообщил, что уже был у нас — "
                             f"поблагодарить и попросить оставить отзыв на Авито "
                             f"({(l.get('name') or 'сделка')[:50]}). Далее: отзыв "
                             "оставлен → заметка в сделку",
                             BADRI, nowts + 4 * 3600)
                    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common",
                        "params": {"text": "Клиент пишет, что уже был у нас — по "
                                           "правилу владельца 11.11 ставим задачу "
                                           "попросить отзыв."}}])
                    done["human"] += 1
                    continue
        last_in = max((m.get("created") or 0) for m in ins) if ins else 0
        last_out = max((m.get("created") or 0) for m in outs) if outs else 0
        nowts = int(now.timestamp())
        # F34 (кейс Радик 13.11): исходящее в чате, но БЕЗ заметки
        # «Отправлено клиенту» — ручное сообщение менеджера из приложения
        # Авито. Это касание человека: переносим антиспам-зазор на него —
        # автодожим поверх живого диалога запрещён (перегруз клиента).
        manual_outs = [m for m in outs if (m.get("created") or 0) > last_sent_ts]
        if manual_outs:
            mo = max(manual_outs, key=lambda m: m.get("created") or 0)
            last_sent_ts = mo.get("created") or last_sent_ts
            mt = (msg_text(mo) or "")[:60]
            if mt and not any("(manual-авто)" in (((n.get("params") or {}).get("text") or ""))
                              and mt[:25] in (((n.get("params") or {}).get("text") or ""))
                              for n in notes):
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"(manual-авто): ручное сообщение менеджера в чате Авито «{mt}» "
                            f"({datetime.fromtimestamp(mo.get('created') or 0, MSK).strftime('%d.%m %H:%M')}) — "
                            f"зазор перенесён на него, автодожимы поверх не шлём."}}])
        # F27 (кейс Егор Пилипец 12.11): клиент сообщил, что УЖЕ приехал /
        # ждёт на месте → сделка в «Приехал», контрольные задачи закрываем.
        # Раньше таких слов не было ни в одной регулярке — сделка висела
        # в «Приедет», пока человек не перевёл руками.
        if l.get("status_id") in (ST_FIRST, ST_TEPLY, ST_PRIEDET) and ins:
            arr_txt = " ".join(msg_text(m) for m in ins[:3])
            if ARRIVED_RE.search(arr_txt):
                amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_PRIEHAL}])
                close_my = open_tasks_by_id(lid)
                mine = [t for t in close_my
                        if (t.get("text") or "").startswith(("Kimi для", "Далее:"))]
                if mine:
                    amo("PATCH", "/api/v4/tasks",
                        [{"id": t["id"], "is_completed": True} for t in mine])
                    _task_cache.pop(lid, None)
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"Клиент сообщил о приезде (F27): «{arr_txt[:100]}» — сделка переведена "
                            f"в «Приехал», контрольные задачи закрыты."}}])
                done["confirm"] += 1
                continue
        # F28 (кейс Кирилл Тех 12.11): контрольная задача «за день до приезда
        # подтвердить запись» ПРОСРОЧЕНА — человек не успел, а движок молчал:
        # лестница «актуален?» только для бездатных, датные ждали человека.
        # Теперь движок сам шлёт утверждённое подтверждение (регламент S4)
        # и переставляет контроль на 9:00 следующего дня.
        if l.get("status_id") == ST_PRIEDET and cid:
            if any(("за день до приезда" in (t.get("text") or "")
                    or "подтвердить запись" in (t.get("text") or ""))
                   and (t.get("complete_till") or 0) < nowts
                   for t in tasks):
                if not any("confirm_day" in (((n.get("params") or {}).get("text") or ""))
                           for n in notes):
                    send(lid, cid, T_CONFIRM_DAY, "confirm_day")
                    d9 = (now + timedelta(days=1)).date()
                    add_task(lid,
                             "Kimi для Константина: подтверждение «завтра ждём» отправлено "
                             f"({(l.get('name') or 'сделка')[:50]}). День приезда: приехал → «Приехал»; "
                             "молчит к вечеру → «Вы где?», потом перенос или закрытие",
                             KONSTANTIN,
                             datetime(d9.year, d9.month, d9.day, 9, 0,
                                      tzinfo=MSK).timestamp())
                    done["human"] += 1
                    continue
        # open_tasks(l) НЕ работает: amoCRM не встраивает tasks в lead
        # (with=tasks игнорируется) — проверяем открытые задачи через API
        tasks = open_tasks_by_id(lid)
        try:
            # финал — отказ сервиса («таким не занимаемся») + «понял» клиента:
            # сделка живёт по ошибке — закрываем (кейс Данил 06.11)
            # ВАЖНО: messenger/v3 отдаёт сообщения ОТ НОВЫХ К СТАРЫМ —
            # берём [:2]/первое, а не [-2:]/reversed (кейс Юлиана 07.11:
            # движок смотрел САМОЕ ПЕРВОЕ сообщение и слал confirm по «вечеру»)
            our_last2 = [msg_text(m) for m in outs][:2]
            client_last_txt = next((msg_text(m) for m in msgs
                                    if m.get("direction") == "in"), "")
            if (last_out > last_in and client_last_txt
                    and any(REFUSAL_RE.search(t or "") for t in our_last2)
                    and ACK_RE.search(client_last_txt)):
                close_lead(lid, LOSS_NE_NASH_PROFIL,
                           "Автозакрытие движка дожима: финал переписки — отказ "
                           "сервиса («не занимаемся/не делаем»), клиент подтвердил "
                           "вежливо. Сделка оказалась в воронке по ошибке.")
                done["refusal"] += 1
                continue
            # отказ сервиса по профилю + клиент молчит > суток → закрываем
            # и без подтверждения «понял» (кейс Кирилл/редуктор 10.11:
            # «не делаем» отправлено, клиент промолчал — сделка висела бы вечно)
            if (last_out > last_in and ins and client_last_txt
                    and any(REFUSAL_RE.search(t or "") for t in our_last2)
                    and nowts - last_in >= 24 * 3600):
                close_lead(lid, LOSS_NE_NASH_PROFIL,
                           "Автозакрытие движка дожима: последняя реплика сервиса — "
                           "отказ по профилю («не делаем/не занимаемся»), клиент "
                           "молчит более суток. Закрыто без ожидания подтверждения.")
                done["refusal"] += 1
                continue
            if no_show:
                # отметка «не приехал» человеком: перенос или закрытие — за человеком
                if not any("перенос" in (t.get("text") or "").lower()
                           or "не приехал" in (t.get("text") or "").lower()
                           for t in tasks):
                    add_task(lid, "Kimi для Константина: клиент НЕ ПРИЕХАЛ "
                             f"({(l.get('name') or 'сделка')[:50]}) — уточнить "
                             "перенос или закрыть. Далее: молчит сутки после "
                             "уточнения → прощание и закрытие «Пропала "
                             "потребность»", nowts + 3600)
                    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                        "text": "Отметка «не приехал» — движок не дожимает, "
                                "поставлена задача уточнить перенос или закрыть."}}])
                    done["human"] += 1
                continue
            # F13 (кейс Надежда 11.11): ожидание «сам наберёт» (sam_due) не должно
            # блокировать НОВОЕ сообщение клиента — клиентка сама вернулась
            # («сегодня около 10-11 можно приехать?»), а движок её игнорировал.
            if sam_due and nowts < sam_due and last_in <= last_out:
                continue  # клиент просил сам набрать — ждём срока, пока он молчит
            # дожим проигнорирован ≥ суток → прощаемся и закрываем (владелец 06.11)
            if (last_key and any(k in last_key for k in BYE_KEYS)
                    and last_in < last_sent_ts
                    and nowts - last_sent_ts >= 24 * 3600):
                if send(lid, cid, T_BYE, "bye"):
                    close_lead(lid, LOSS_PROPALA,
                               "Дожим не получил ответа более суток — отправлено "
                               "прощальное сообщение, сделка закрыта «Пропала "
                               "потребность» (схема владельца 06.11).")
                continue
            # антиспам-зазор: после любой нашей отправки ≥FUP_GAP ч тишины
            if n_sent > 0 and nowts - last_sent_ts < FUP_GAP * 3600:
                continue
            if sam_due and nowts >= sam_due:
                # возврат после «сам наберёт» — ОДИН раз: после него работает
                # общая лестница (кейс Максим/шлифовка 10.11: soft уходил
                # каждые ~20 ч бесконечно, прощание с закрытием было
                # недостижимо — 24 ч тишины после soft не наступали никогда)
                if not any(ts > sam_due for ts in fup_ts):
                    send(lid, cid, T_SOFT, "soft")
                else:
                    # F36 (указание владельца 14.11 «да-да нет-нет закрываем»):
                    # после напоминания клиент ответил вялым подтверждением
                    # («да/да-да/хорошо/ок») и снова пропал ≥ суток — слов нет,
                    # закрываем с прощанием.
                    if (client_last_txt and last_in > sam_due
                            and last_in < last_sent_ts
                            and nowts - last_in >= 24 * 3600
                            and len(client_last_txt) <= 25 and "?" not in client_last_txt
                            and ACK_ANY_RE.match(client_last_txt.strip())):
                        if send(lid, cid, T_BYE, "bye"):
                            close_lead(lid, LOSS_PROPALA,
                                       "После напоминания (сам наберёт) клиент ответил вялым "
                                       "подтверждением и пропал на сутки — прощание и закрытие "
                                       "«Пропала потребность» (правило владельца 14.11, F36).")
                continue
            if last_in > last_out:  # клиент ждёт нашего ответа
                if nowts - last_in < 2 * 3600:
                    continue
                last_client = next(msg_text(m) for m in msgs
                                   if m.get("direction") == "in")
                # клиент сам закрыл вопрос → закрываем сделку, не пишем ему
                if CLOSE_RE.search(last_client):
                    loss = (LOSS_DRUGIE if ("друг" in last_client.lower()
                                            or "у других" in last_client.lower())
                            else LOSS_PROPALA)
                    close_lead(lid, loss,
                               f"Клиент сам закрыл вопрос: «{last_client[:120]}» "
                               f"— сделка закрыта автоматически (правило владельца 06.11).")
                    continue
                # прислан VIN → считаем стоимость (правило владельца 06.11:
                # «прислали VIN — задача Бадри посчитать стоимость», кейс Юлиана)
                if has_vin(last_client):
                    if not tasks:
                        add_task(lid,
                                 "Kimi для Бадри: посчитать стоимость по VIN из чата Авито "
                                 f"({(l.get('name') or 'сделка')[:60]}). Далее: назвать цену "
                                 "в чате → уточнить время приезда → запросить телефон",
                                 next_9_msk(), BADRI)
                        done["human"] += 1
                    continue
                # клиент «вот-вот приедет» → не пишем ничего, вечером «Приехал?»
                if PATIENT_RE.search(last_client):
                    if not tasks:
                        eve = now.replace(hour=19, minute=0, second=0,
                                          microsecond=0)
                        if now >= eve:
                            eve += timedelta(days=1)
                        add_task(lid, "Kimi для Константина: клиент обещал "
                                 "подъехать (ждёт/в пути) — написать «всё "
                                 f"получается? Приедете?» ({(l.get('name') or 'сделка')[:50]}). "
                                 "Далее: подтвердил → «Приехал»; перенос → новая дата.",
                                 int(eve.timestamp()))
                        amo("POST", f"/api/v4/leads/{lid}/notes",
                            [{"note_type": "common", "params": {"text":
                                "Клиент «вот-вот приедёт» (прикурит/в пути) — "
                                "автодожимы запрещены, задача «Приехал?» на вечер."}}])
                        done["human"] += 1
                    continue
                if SAM_NABERET_RE.search(last_client):
                    due = last_in + 4 * 86400
                    amo("POST", f"/api/v4/leads/{lid}/notes",
                        [{"note_type": "common", "params": {"text":
                            f"Клиент «сам наберёт — вернуться» после {due} "
                            f"(4 дня, схема владельца). Дожим до срока не шлём."}}])
                    if not tasks:
                        add_task(lid,
                                 "Kimi для Константина: написать клиенту "
                                 f"({(l.get('name') or 'сделка')[:50]}; обещал сам набрать, "
                                 "прошло 4 дня) — мягкий возврат в диалог. Далее: молчит "
                                 "сутки после возврата → «видим, что вопрос не актуален» "
                                 "и закрытие", due)
                    done["sam_naberet"] += 1
                    continue
                if "перенес" in last_client.lower() or "не получится" in \
                        last_client.lower() or "переношу" in last_client.lower():
                    send(lid, cid, T_NEWDATE, "newdate")
                    # F14 (кейсы Даниил/Александр 11.11): задача «за день до
                    # приезда» при неизвестной дате — ерунда. Заменяем контрольную
                    # задачу на «ждём новую дату» (add_task сам закрывает старую).
                    add_task(lid,
                             "Kimi для Константина: клиент просил перенос — "
                             f"ждём новую дату ({(l.get('name') or 'сделка')[:50]}). "
                             "Далее: назвал дату → «Приедет» + задача за день; молчит "
                             "2 суток → «актуален?» по схеме",
                             nowts + 2 * 86400)
                    continue
                if SCHED_RE.search(last_client):
                    # клиент ждёт просчёт → не приглашаем, пока цена не названа
                    if any("посчитать стоимость" in (t.get("text") or "")
                           for t in tasks):
                        amo("POST", f"/api/v4/leads/{lid}/notes",
                            [{"note_type": "common", "params": {"text":
                                "Клиент назвал время, но ждёт просчёт стоимости "
                                "(открытая задача на расчёт) — приглашение НЕ "
                                "отправлено, сначала цена (правило владельца 07.11)."}}])
                        continue
                    if not any("Отправлено клиенту" in ((n.get("params") or {})
                               .get("text") or "") and
                               (n.get("created_at") or 0) > last_in
                               for n in notes):
                        if not send(lid, cid, T_CONFIRM, "confirm"):
                            continue
                        amo("PATCH", "/api/v4/leads", [{"id": lid,
                             "status_id": ST_PRIEDET,
                             "responsible_user_id": KONSTANTIN}])
                        d = (now + timedelta(days=1)).date()
                        add_task(lid, "Kimi для Константина: за день до приезда написать "
                                 "«всё получается? Приедете?» "
                                 f"({(l.get('name') or 'сделка')[:50]}). Далее: подтвердил → "
                                 "«Приехал» по факту; перенос → новая дата + задача",
                                 datetime(d.year, d.month, d.day, 10, 0,
                                          tzinfo=MSK).timestamp())
                    continue
                # вопрос по сути — нужен человек
                # своя задача на ответ по существу, а не «любая открытая»:
                # иначе висящая «Когда приедет?» навсегда блокирует ветку
                # (кейс Давид 10.11: «Пружин?» — содержательной задачи не
                # появилось, ответ ушёл формальный «Я всё написал»)
                # F17: короткое подтверждение без вопроса («Отлично», «Принял,
                # спасибо») — не вопрос по существу, задачу не ставим
                if (len(last_client) <= 30 and "?" not in last_client
                        and ACK_ANY_RE.match(last_client)):
                    continue
                # F17-доп (кейс whtevr 13.11): сообщение — только номер телефона
                # (клиент оставляет номер по запросу) — это НЕ вопрос по существу.
                # Иначе суть-задача пересоздаётся, блокирует дожим (F11) и сделка
                # с датой приезда зависает без подтверждения.
                if norm_phone(last_client) or (len(last_client) <= 20
                        and PHONE_SCAN_RE.search(last_client)):
                    continue
                if not any("по существу" in (t.get("text") or "").lower()
                           or "ответить в чате" in (t.get("text") or "").lower()
                           for t in tasks):
                    # вопросы по услуге (не цена/не время) — Бадри, не Константин.
                    # F32 (кейс Салим 13.11): стоимость ПЕРЕЧИСЛЕНИЯ работ/узлов
                    # («сайлентблоки 2 передних, 2 задних, втулки, косточки») —
                    # технический просчёт, требует мастера, а не администратора:
                    # маршрутизируем на Бадри (по указанию владельца).
                    if "?" in last_client and not PRICE_ASK_RE.search(last_client):
                        to_badri = True
                    elif (PRICE_ASK_RE.search(last_client)
                            and (last_client.count(",") >= 1
                                 or len(re.findall(r"\bи\b", last_client.lower())) >= 1)):
                        to_badri = True
                    else:
                        to_badri = False
                    who = BADRI if to_badri else KONSTANTIN
                    whom = "Бадри" if to_badri else "Константина"
                    add_task(lid,
                             f"Kimi для {whom}: Ответить в чате Авито — клиент ждёт "
                             f"ответа по сути ({(l.get('name') or 'сделка')[:50]}). "
                             "Далее: ответили → уточнить время приезда → «Приедет»; "
                             "вопрос про цену — назвать цену, не запрашивать телефон раньше. "
                             "Пока не отвечено — задача переносится на следующий рабочий "
                             "день 10:00 автоматически (правило владельца 07.11)",
                             nowts + 3 * 3600, who)
                    done["human"] += 1
                continue
            # мы ответили последними — клиент молчит
            # дыра «запись состоялась» (кейс Егор 07.11: «давайте на 10-11» →
            # сервис «подъезжайте»): формально последняя реплика наша, но клиент
            # назвал день/время и сервис подтвердил — сделка должна быть в
            # «Приедет», а не дожиматься «актуален?»
            last_ins_txt = [msg_text(m) for m in ins[:4]]
            joint_in = " ".join(t or "" for t in last_ins_txt)
            # клиент сам закрыл вопрос, даже если последней репликой ответил
            # сервис (кейс Даниил 10.11: «не актуально» → сервис «Спасибо
            # большое» — CLOSE_RE стоял только в ветке «клиент ждёт ответа»
            # и сделка висела открытой)
            if client_last_txt and CLOSE_RE.search(client_last_txt):
                loss = (LOSS_DRUGIE if "друг" in client_last_txt.lower()
                        else LOSS_PROPALA)
                close_lead(lid, loss,
                           f"Клиент сам закрыл вопрос: «{client_last_txt[:120]}» — "
                           f"сделка закрыта автоматически (правило владельца 06.11).")
                done["closed"] += 1
                continue
            if (l.get("status_id") != ST_PRIEDET and last_ins_txt
                    and any(SCHED_RE.search(t or "") for t in last_ins_txt)
                    and not CLOSE_RE.search(joint_in)
                    and not re.search(r"отмен|не получится|перенес|переношу|"
                                      r"передумал|или", joint_in, re.IGNORECASE)):
                amo("PATCH", "/api/v4/leads", [{"id": lid,
                     "status_id": ST_PRIEDET,
                     "responsible_user_id": KONSTANTIN}])
                d = (now + timedelta(days=1)).date()
                add_task(lid, "Kimi для Константина: за день до приезда подтвердить "
                         "запись «всё получается? Приедете?» "
                         f"({(l.get('name') or 'сделка')[:50]}). Далее: подтвердил → "
                         "«Приехал» по факту; перенос → новая дата + задача",
                         datetime(d.year, d.month, d.day, 10, 0,
                                  tzinfo=MSK).timestamp())
                amo("POST", f"/api/v4/leads/{lid}/notes",
                    [{"note_type": "common", "params": {"text":
                        "Перевод в «Приедет» движком дожима: в последних сообщениях "
                        "клиент назвал день/время приезда, сервис подтвердил. "
                        "Задача — подтвердить запись за день.\n"
                        f"Сработавшие реплики клиента: «{joint_in[:150]}»"}}])
                done["confirm"] += 1
                continue
            # F26 (кейсы Денис/Даниил 12.11): «дожать вручную» — задача человеку.
            # Если она ПРОСРОЧЕНА и клиент с тех пор молчит — человек не дожал,
            # ждать больше нечего: прощание + закрытие «Пропала потребность»
            # (раньше сделка зависала навсегда: касания исчерпаны, задача красная,
            # движок ждал действий, которых не было).
            manual_tasks = [t for t in tasks
                            if "дожать" in (t.get("text") or "").lower()
                            or "вручную" in (t.get("text") or "").lower()]
            overdue_manual = [t for t in manual_tasks
                              if (t.get("complete_till") or 0) < nowts]
            if overdue_manual and cid:
                last_due = max(t.get("complete_till") or 0 for t in overdue_manual)
                if last_in < last_due and nowts - last_due >= 3600:
                    if not send(lid, cid, T_BYE, "bye"):
                        continue
                    close_lead(lid, LOSS_PROPALA,
                               "Ручной дожим не дал ответа (задача просрочена, клиент молчит) — "
                               "прощание отправлено, сделка закрыта «Пропала потребность» "
                               "(финал после ручного дожима, F26).")
                    done["bye"] += 1
                    continue
            # клиент «вот-вот приедет» — не долбим вообще (кейс Никита)
            if PATIENT_RE.search(joint_in):
                continue
            # F29 (кейс Евгений Иванович 12.11): висит задача «ответить по
            # существу», но на вопрос клиента сервис УЖЕ ответил — вопрос снят
            # нашим ответом. Автоматика молчала (F11 видел висящую задачу), а
            # задача краснела. Закрываем её, контрольная — «Когда приедет?» на
            # завтра. Срабатывает только если ответ новее последнего вопроса.
            qs = [m for m in ins if "?" in (msg_text(m) or "")]
            if qs and any("по существу" in (t.get("text") or "").lower()
                          or "ответить в чате" in (t.get("text") or "").lower()
                          for t in open_tasks_by_id(lid)):
                q = max(qs, key=lambda m: m.get("created") or 0)
                if any((m.get("created") or 0) >= (q.get("created") or 0)
                       for m in outs):
                    mine = [t for t in open_tasks_by_id(lid)
                            if (t.get("text") or "").startswith(("Kimi для", "Далее:"))]
                    amo("PATCH", "/api/v4/tasks",
                        [{"id": t["id"], "is_completed": True} for t in mine])
                    _task_cache.pop(lid, None)
                    add_task(lid,
                             "Kimi для Константина: Когда приедет? "
                             f"({(l.get('name') or 'сделка')[:50]}). Далее: назвал "
                             "дату → «Приедет» + контроль; молчит → дожим по схеме",
                             KONSTANTIN, tomorrow_10_msk())
                    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                        "text": "F29: на вопрос клиента («" + (msg_text(q) or "")[:80] +
                                "») сервис уже ответил — задача «ответить по существу» "
                                "закрыта автоматически, контрольная на завтра."}}])
                    done["human"] += 1
                    continue
            # F11 (кейс Давид 11.11): висит открытая задача «ответить по существу»
            # — значит наш последний ответ вопрос клиента НЕ закрыл (Давид спросил
            # цену ручек+масла, получил ответ про пружины и «Я всё написал»).
            # Дожим «актуален?» в таком состоянии — формальный пинок человеку,
            # который остался без ответа: шаблоны не шлём, пока человек не
            # ответит по существу (ветка «вопрос по сути» ставит такую задачу).
            if any("по существу" in (t.get("text") or "").lower()
                   or "ответить в чате" in (t.get("text") or "").lower()
                   for t in tasks):
                continue
            silence = nowts - max(last_out, last_sent_ts)
            if n_sent >= 2 and silence >= 96 * 3600:
                if not has_manual_task(tasks):
                    add_task(lid, "Kimi для Константина: дожать сделку вручную — "
                             f"2 автокасания не дали ответа ({(l.get('name') or 'сделка')[:50]}). "
                             "Далее: нет ответа и после ручного дожима → «видим, что вопрос "
                             "не актуален» и закрытие",
                             nowts + 86400)
                    done["manual"] += 1
                continue
            if n_sent == 0 and silence >= FUP_SILENCE_FIRST * 3600:
                if l.get("status_id") == ST_PRIEDET:
                    if re.search(r"перенес|переношу|не получится|передумал|"
                                 r"не смогу|по обстоятельствам", joint_in,
                                 re.IGNORECASE):
                        send(lid, cid, T_NEWDATE, "newdate")
                        add_task(lid,
                                 "Kimi для Константина: клиент просил перенос — "
                                 f"ждём новую дату ({(l.get('name') or 'сделка')[:50]}). "
                                 "Далее: назвал дату → «Приедет» + задача за день; "
                                 "молчит 2 суток → «актуален?» по схеме",
                                 nowts + 2 * 86400)
                    else:
                        # F15 (кейс Егор-капремонт 11.11): «Приедет» + тишина
                        # без даты — ветка молчала, добавляя только задачу
                        # («подтвердить запись» ещё и блокировала всё, т.к.
                        # содержит слова «подтвердить запись»). По схеме
                        # владельца здесь идёт лестница: «актуален?» → сутки
                        # тишины → прощание + закрытие (BYE_KEYS уже умеет).
                        send(lid, cid, T_ACTUAL, "actual")
                        add_task(lid,
                                 "Kimi для Константина: «актуален?» отправлено "
                                 f"({(l.get('name') or 'сделка')[:50]}). Далее: "
                                 "молчит сутки → прощание и закрытие «Пропала "
                                 "потребность»; ответил → диалог по схеме",
                                 nowts + 24 * 3600)
                else:
                    send(lid, cid, T_ACTUAL, "actual")
            elif n_sent >= 1 and silence >= 72 * 3600:
                if not has_manual_task(tasks):
                    add_task(lid, "Kimi для Константина: дожать сделку вручную — "
                             f"автокасания не дали ответа ({(l.get('name') or 'сделка')[:50]}). "
                             "Далее: нет ответа и после ручного дожима → «видим, что вопрос "
                             "не актуален» и закрытие",
                             nowts + 86400)
                    done["manual"] += 1
            else:
                done["skip"] += 1
        except Exception as e:
            log(f"  дожим+ {lid}: ошибка ({e})")
    log(f"дожим+ движок: {json.dumps(done, ensure_ascii=False)}")
    return done


def run():
    now = datetime.now(MSK)
    log(f"=== Чат-контур v2 {'(DRY_RUN) ' if DRY_RUN else ''}{now.strftime('%d.%m.%Y %H:%M')} мск ===")
    if not AMO_TOKEN:
        raise RuntimeError("AMO_TOKEN не задан")
    if not KIMI_KEY:
        raise RuntimeError("MOONSHOT_API_KEY не задан")

    # рабочее окно (мск): клиентам не пишем ночью, поэтому вне окна
    # весь чат-контур молчит (как razbor.py). В DRY_RUN окно не действует.
    # F20 (кейс Артур 11.11): заявки 20:00–21:00 ПРИНИМАЕМ (задачи, этапы),
    # но клиенту не пишем — отправки откладываются на утро (как NO_SEND).
    if not DRY_RUN:
        def _hm(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        cur = now.hour * 60 + now.minute
        if not (_hm(os.environ.get("WINDOW_START", "9:00")) <= cur
                <= _hm(os.environ.get("CHAT_WINDOW_END", "21:00"))):
            log("вне рабочего окна — выход без действий")
            print("SUMMARY_JSON:" + json.dumps(
                {"run": now.strftime("%d.%m.%Y %H:%M"), "window_skip": True},
                ensure_ascii=False))
            return {"run": now.strftime("%d.%m.%Y %H:%M"), "window_skip": True}
    eff_no_send = NO_SEND or now.hour >= 20  # F20: вечер принимаем, не пишем

    tok = avito_token()
    acc = amo_get("/api/v4/account")
    log(f"amo: {acc.get('name') or acc.get('subdomain') or 'ok'}; avito: токен получен")

    from_ts = int((now - timedelta(days=WINDOW_DAYS)).timestamp())
    chats = http("GET",
                 f"https://api.avito.ru/messenger/v2/accounts/{AVITO_UID}/chats?limit=100",
                 token=tok).get("chats") or []
    fresh = [c for c in chats if (c.get("updated") or 0) >= from_ts]
    log(f"чатов в выдаче: {len(chats)}, свежих за {WINDOW_DAYS} дн: {len(fresh)}")

    messages = {}
    for c in fresh:
        cid = c["id"]
        try:
            m = http("GET",
                     f"https://api.avito.ru/messenger/v3/accounts/{AVITO_UID}/chats/{cid}/messages/",
                     token=tok)
            messages[cid] = m.get("messages", [])
        except Exception as e:
            log(f"  чат {cid[:16]}…: не прочитан ({e})")
        time.sleep(0.1)

    by_name = {}
    for c in fresh:
        # F10: ключи — имена ВСЕХ участников, кроме нашего профиля. Раньше
        # брался только users[0] — а там может быть сам сервис, а клиент —
        # users[1] (кейс Дмитрий(Рябуха) 11.11: чат существовал, подбор fails).
        for u in (c.get("users") or []):
            nm = (u.get("name") or "").strip().lower()
            if not nm:
                continue
            by_name.setdefault(nm, []).append(c)
        by_name.setdefault("*", []).append(c)  # fallback-пул для поиска по времени

    # F23: подхват сделок, принятых интеграцией Авито напрямую (мимо лотка) —
    # без этого они невидимы для дожима (кейс «Замена амортизаторов» 11.11)
    try:
        adopt_integration_leads(tok, by_name, now)
    except Exception as e:
        log(f"усыновление: ошибка ({e})")
    # F33: просьба об отзыве у успешно реализованных чат-сделок (раз на сделку)
    try:
        ask_reviews(tok, now)
    except Exception as e:
        log(f"отзывы: ошибка ({e})")

    unsorted = [u for u in embedded(amo_get("/api/v4/leads/unsorted?limit=200"), "unsorted")
                if "avito" in (u.get("source_name") or "").lower()]
    log(f"чат-заявок в «Неразобранном»: {len(unsorted)} (беру до {MAX_PER_RUN})")

    # FIFO: берём САМЫЕ СТАРЫЕ заявки первыми — иначе хвост (старые чаты)
    # голодал: свежие падали сверху списка и съедали лимит каждый прогон.
    unsorted.sort(key=lambda u: u.get("created_at") or 0)

    # антидубль внутри прогона: один клиент = один avito user id.
    # Клиент может написать с НЕСКОЛЬКИХ объявлений — придёт несколько заявок
    # в лоток; без этой проверки контур создаст по сделке на каждое объявление
    # (кейс Юлианны 05.11: две сделки «Шиномонтаж» с разницей в минуту).
    def chat_uid(c):
        # F10: users[0] может быть наш профиль, а не клиент (кейс Дмитрий(Рябуха))
        # — тогда все чаты слипнутся в одного «клиента». Берём первого НЕ нашего.
        try:
            for u in c.get("users") or []:
                if u.get("id") != AVITO_UID and u.get("id"):
                    return u.get("id")
            return None
        except (KeyError, IndexError, TypeError):
            return None

    seen_uid = {}  # avito user id -> id созданной сделки

    acted, skipped, errors = [], [], []
    for item in unsorted[:MAX_PER_RUN]:
        uid = item.get("uid")
        md = item.get("metadata") or {}
        cname = md.get("from") or "?"
        try:
            chat, delta = find_chat_for_item(item, by_name)
            if not chat:
                skipped.append({"uid": uid, "reason": f"чат не найден (Δ{int(delta)}с)"})
                continue
            cid = chat["id"]
            msgs = messages.get(cid) or []
            outs = [m for m in msgs if m.get("direction") == "out"]
            if not outs:
                skipped.append({"uid": uid, "reason": "Бадри ещё не отвечал — агент молчит"})
                log(f"  {cname}: Бадри ещё не отвечал — пропуск")
                continue

            # F38 (кейс Василий 14.11): клиент написал из ВТОРОГО чата — без
            # этой проверки появится вторая сделка на того же человека.
            # Ищем открытую чат-сделку клиента; нашли — новую принимаем и
            # сразу закрываем «Дубль», а второй чат приклеиваем к основной.
            dup_client = None
            try:
                import urllib.parse as _up
                cands = embedded(amo_get(f"/api/v4/leads?query={_up.quote(cname)}&limit=10"), "leads")
                for c in cands:
                    if not c.get("id") or c.get("status_id") in (ST_CLOSED, 142):
                        continue
                    tags_c = [t.get("name") for t in (c.get("_embedded") or {}).get("tags", [])]
                    if TAG_CHAT in tags_c and cname.lower() in (c.get("name") or "").lower():
                        dup_client = c["id"]
                        break
            except Exception:
                dup_client = None
            if dup_client:
                if DRY_RUN:
                    log(f"  DRY_RUN {cname}: дубль клиента — открытая сделка {dup_client}")
                    skipped.append({"uid": uid, "reason": f"дубль клиента, сделка {dup_client}"})
                    continue
                acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept",
                          {"user_id": BADRI, "status_id": ST_FIRST})
                new_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
                if new_id:
                    amo("PATCH", "/api/v4/leads", [{"id": new_id, "status_id": ST_CLOSED,
                                                    "loss_reason_id": LOSS_DUBL}])
                    amo("POST", f"/api/v4/leads/{new_id}/notes", [{"note_type": "common", "params": {
                        "text": f"Дубль клиента: {cname} писал из второго чата. Основная сделка "
                                f"{dup_client}. Закрыто автоматически (F38)."}}])
                    amo("POST", f"/api/v4/leads/{dup_client}/notes", [{"note_type": "common", "params": {
                        "text": f"F38: клиент писал из ВТОРОГО чата ({cid}) — чат приклеён к этой сделке, "
                                f"дубль закрыт. chat_id={cid}"}}])
                    amo("POST", "/api/v4/tasks", [{
                        "text": f"Kimi для Константина: клиент {cname} писал из второго чата — "
                                f"проверить переписку ({cid[:16]}…), актуальный запрос во втором чате.",
                        "responsible_user_id": KONSTANTIN,
                        "complete_till": tomorrow_10_msk(),
                        "entity_id": dup_client, "entity_type": "leads"}])
                log(f"  {cname}: дубль клиента — сделка {dup_client}, новая заявка закрыта")
                acted.append({"lead": new_id, "client": cname, "interest": None, "dup_of": dup_client})
                continue

            # антидубль: тот же клиент (avito user id) уже обработан в этом прогоне
            au = chat_uid(chat)
            if au and au in seen_uid:
                if DRY_RUN:
                    log(f"  DRY_RUN {cname}: дубль клиента (уже сделка {seen_uid[au]})")
                    skipped.append({"uid": uid, "reason": f"дубль клиента, сделка {seen_uid[au]}"})
                    continue
                acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept",
                          {"user_id": BADRI, "status_id": ST_FIRST})
                dup_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
                if dup_id:
                    amo("PATCH", "/api/v4/leads", [{"id": dup_id, "status_id": ST_CLOSED,
                                                    "loss_reason_id": 24765102}])  # Дубль
                    amo("POST", f"/api/v4/leads/{dup_id}/notes", [{"note_type": "common", "params": {
                        "text": f"Дубль: клиент {cname} писал с нескольких объявлений. "
                                f"Основная сделка: {seen_uid[au]}. Закрыто автоматически чат-контуром."}}])
                log(f"  {cname}: дубль клиента — принят и закрыт (основная сделка {seen_uid[au]})")
                acted.append({"lead": dup_id, "client": cname, "interest": None, "dup_of": seen_uid[au]})
                continue

            parts = []
            # messenger/v3 отдаёт сообщения ОТ НОВЫХ К СТАРЫМ — для транскрипта
            # разворачиваем в хронологию (кейс Юлиана 07.11)
            for m in reversed(msgs):
                who = "КЛИЕНТ" if m.get("direction") == "in" else "СЕРВИС"
                t = msg_text(m)
                if t:
                    parts.append(f"{who}: {t}")
            transcript = "\n".join(parts)
            if len(transcript) < 10:
                skipped.append({"uid": uid, "reason": "пустая переписка"})
                continue

            item_title = (((chat.get("context") or {}).get("value") or {}).get("title")) or "услуга сервиса"
            r = classify(transcript, cname, item_title)
            interest = bool(r.get("interest"))
            # детерминированная страховка (кейс Данил 06.11): финал — отказ
            # сервиса + вежливое «понял» клиента → интереса нет вне зависимости
            # от того, что решил LLM по первому вопросу
            our_last = [msg_text(m) for m in msgs
                        if m.get("direction") == "out"][:2]
            client_last = next((msg_text(m) for m in msgs
                                if m.get("direction") == "in"), "")
            if (interest and client_last
                    and any(REFUSAL_RE.search(t or "") for t in our_last)
                    and ACK_RE.search(client_last)):
                interest = False
                r["summary"] = ((r.get("summary") or "")
                                + " [переопределено: финал — отказ сервиса]")
            # страховка 2 (кейс Игорь 07.11): ПОСЛЕДНЯЯ реплика в чате — отказ
            # сервиса, а клиент после него промолчал → интереса нет: не принимаем
            # в воронку и не дожимаем «актуален?» человека, которому отказали.
            last_msg = msgs[0] if msgs else {}  # msgs: новые первые
            if (interest and last_msg.get("direction") == "out"
                    and REFUSAL_RE.search(msg_text(last_msg) or "")):
                interest = False
                r["summary"] = ((r.get("summary") or "")
                                + " [переопределено: последняя реплика — отказ сервиса, "
                          "клиент после него не писал]")
            # страховка 3 (кейс Дмитрий 13.11): клиент получил цену/ответ и
            # молчит МЕНЬШЕ 20 часов — это тишина, а не отказ. Закрывать
            # «пропал после цены» можно только по старым диалогам: LLM иначе
            # закрывает свежий контакт через 15 минут после нашего ответа.
            if not interest:
                last_out_ts = next((m.get("created") or 0 for m in msgs
                                    if m.get("direction") == "out"), 0)
                reason_txt = (r.get("reason") or "") + " " + (r.get("summary") or "")
                if (int(now.timestamp()) - last_out_ts) < FUP_SILENCE_FIRST * 3600 and re.search(
                        r"пропал|не ответил|молчит|уш[её]л|ушел", reason_txt, re.IGNORECASE):
                    interest = True
                    r["summary"] = ((r.get("summary") or "")
                        + " [переопределено: свежая тишина после нашего ответа — "
                          "это не отказ, дожим по схеме]")
            request = (r.get("request") or "").strip()
            lead_name = f"{cname} — {request[:60]}" if request else f"Авито — {cname}"
            phone = norm_phone(r.get("phone") or "")
            if not phone:
                # F18: LLM иногда теряет номер — детерминированный скан чата
                phone = scan_phone(msgs)
            # сервис ответил только приветствием → интерес не «разобран»
            greeting_only = bool(outs) and all(
                GREETING_RE.match(msg_text(m) or "") for m in outs)

            if DRY_RUN:
                log(f"  DRY_RUN {cname}: interest={interest}, next={r.get('next_step')}, phone={phone or '—'}")
                acted.append({"client": cname, "interest": interest, "dry": True})
                continue

            # принимаем заявку
            acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept",
                      {"user_id": BADRI, "status_id": ST_FIRST})
            new_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
            if not new_id:
                raise RuntimeError("accept не вернул id сделки")

            note = "\n".join([
                f"Разбор чата Авито: {now.strftime('%d.%m.%Y %H:%M')}",
                f"Объявление: {item_title}",
                f"Клиент: {cname}",
                f"Авто: {r.get('car') or '—'}",
                f"Запрос: {request or '—'}",
                f"Итог: {'интерес' if interest else 'нет интереса'} — {r.get('summary') or ''}",
                f"chat_id={cid}",
            ])

            patch = {"id": new_id, "name": lead_name,
                     "_embedded": {"tags": [{"name": TAG_CHAT}]}}
            tasks = []
            if not interest:
                patch["status_id"] = ST_CLOSED
                patch["loss_reason_id"] = LOSS_NE_NASH_PROFIL
                note += f"\nПричина закрытия: {r.get('reason') or '—'}"
            elif greeting_only:
                # вопрос без ответа по существу — сначала отвечает Бадри
                # (кейс Кирилл/редуктор 10.11: «Здравствуйте» + вопрос про
                # редуктор ушли в «Тёплые» и висели без содержательной задачи)
                patch["status_id"] = ST_TEPLY
                patch["responsible_user_id"] = BADRI
                note += ("\nСервис ответил только приветствием — первичный ответ "
                         "по существу за Бадри.")
                tasks.append(("Kimi для Бадри: ответить по существу в чате Авито — "
                              f"клиент {cname} спросил: {request or '—'}. Далее: "
                              "ответ по сути → уточнить время приезда → «Приедет»; "
                              "не наш профиль → закрыть 143 с причиной.",
                              BADRI, tomorrow_10_msk()))
            else:
                if r.get("said_will_come") or phone:
                    patch["status_id"] = ST_PRIEDET
                    patch["responsible_user_id"] = KONSTANTIN
                    tasks.append(("Kimi для Константина: Приехал? {lead}. Далее: подтвердил "
                                  "приезд → «Приехал»; просит перенос → уточнить новую дату "
                                  "и задачу за день".format(lead=lead_name),
                                  KONSTANTIN, tomorrow_10_msk()))
                else:
                    patch["status_id"] = ST_TEPLY
                    patch["responsible_user_id"] = KONSTANTIN
                    tasks.append(("Kimi для Константина: Когда приедет? {lead}. Далее: назвал "
                                  "дату → «Приедет» + задача за день; молчит → дожим по схеме "
                                  "(«актуален?» → прощание и закрытие)".format(lead=lead_name),
                                  KONSTANTIN, tomorrow_10_msk()))

            amo("PATCH", "/api/v4/leads", [patch])

            # телефон из чата — в контакт
            if phone:
                contact_name = lead_name
                created = amo("POST", "/api/v4/contacts", [{
                    "name": contact_name,
                    "custom_fields_values": [{"field_id": PHONE_FIELD, "values": [{"value": phone}]}],
                    "_embedded": {"leads": [{"id": new_id}]}}])
                note += f"\nТелефон из чата: {phone}"
            elif not interest:
                pass

            # агент сам пишет клиенту — строго по шаблону и только если телефона ещё нет.
            # Ночь (NO_SEND): клиенту не пишем и НИКАКИХ задач на отправку не ставим —
            # в заметку пишем chat_id + шаблон, первый дневной прогон дошлёт сам (send_pending).
            # Одна задача на сделку — только контрольная (решение владельца 06.11).
            sent = None
            pending = None
            if interest and not phone and not greeting_only:
                tpl = None
                if r.get("next_step") == "phone" or r.get("said_will_come"):
                    tpl = T_PHONE
                elif r.get("next_step") == "when":
                    tpl = T_WHEN
                # страховка (кейсы Евгений/Даниил 06.11): если последняя реплика
                # клиента — вопрос о цене/стоимости, а сервис на него не ответил —
                # шаблон про телефон/время не отправляем (нелогично). Оставляем
                # диалог человеку: контрольной задачей будет ответ по существу.
                if tpl:
                    client_last_msg = next((msg_text(m) for m in msgs
                                            if m.get("direction") == "in"), "")
                    our_last_msg = next((msg_text(m) for m in msgs
                                         if m.get("direction") == "out"), "")
                    if (client_last_msg and PRICE_ASK_RE.search(client_last_msg)
                            and not price_answered(our_last_msg)):
                        tpl = None
                        note += ("\nШаблон НЕ отправлен: клиент ждёт ответа по существу "
                                 f"(«{client_last_msg[:80]}») — сначала цена/расчёт, "
                                 "потом телефон/время. Задача контрольная заменена.")
                        tasks = [("Kimi для Константина: ответить по существу в чате "
                                  f"Авито — клиент {cname} ждёт цену/расчёт "
                                  f"(запрос: {request or '—'}). Далее: назвать цену → "
                                  "уточнить время приезда → запросить телефон.",
                                  KONSTANTIN, tomorrow_10_msk())]
                # F34b (кейс Радик 13.11): живой диалог с менеджером — его
                # последняя реплика свежее 2 ч — шаблон не вставляем
                # (перегруз клиента двумя касаниями сразу).
                if tpl:
                    try:
                        _lo = max([(m.get("created") or 0)
                                   for m in msgs if m.get("direction") == "out"] or [0])
                        _li = max([(m.get("created") or 0)
                                   for m in msgs if m.get("direction") == "in"] or [0])
                        if _lo > _li and int(now.timestamp()) - _lo < 2 * 3600:
                            tpl = None
                            note += (f"\nШаблон НЕ отправлен: менеджер ведёт диалог "
                                     f"(его реплика {(int(now.timestamp()) - _lo) // 60} мин назад) — "
                                     "касание человека достаточно.")
                    except Exception:
                        pass
                if tpl:
                    if eff_no_send:
                        pending = (cid, tpl)
                        note += (f"\nШаблон НЕ отправлен (ночь, дождётся дневного прогона), "
                                 f"chat_id={cid}: «{tpl}»")
                    else:
                        http("POST",
                             f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}/chats/{cid}/messages",
                             token=tok, body={"message": {"text": tpl}, "type": "text"})
                        sent = tpl
                        note += f"\nОтправлено клиенту: «{tpl}»"

            amo("POST", f"/api/v4/leads/{new_id}/notes", [
                {"note_type": "common", "params": {"text": note}}])
            if tasks:
                amo("POST", "/api/v4/tasks", [{
                    "text": t[0], "responsible_user_id": t[1],
                    "complete_till": t[2], "entity_id": new_id, "entity_type": "leads"} for t in tasks])

            acted.append({"lead": new_id, "client": cname, "interest": interest,
                          "sent": bool(sent), "pending": bool(pending)})
            if au:
                seen_uid[au] = new_id
            log(f"  {cname}: сделка {new_id}, interest={interest}" + (", написали клиенту" if sent else ""))
        except Exception as e:
            errors.append({"uid": uid, "error": str(e)[:250]})
            log(f"  ОШИБКА {cname}: {e}")

    # дневной дожим ночных шаблонов (NO_SEND ничего не шлёт, только помечает)
    # F31: досылаем только утренним окном 8:00–9:30, как и весь дожим
    followups = 0
    if not DRY_RUN and not eff_no_send and in_daily_send_window(now):
        try:
            followups = send_pending_templates(tok)
        except Exception as e:
            log(f"дожим: ошибка ({e})")
        # регулярный дожим тёплых сделок: эскалация по схеме владельца 06.11
        try:
            followup_engine(tok)
        except Exception as e:
            log(f"дожим+ движок: ошибка ({e})")

    summary = {"run": now.strftime("%d.%m.%Y %H:%M"), "dry_run": DRY_RUN,
               "fresh_chats": len(fresh), "unsorted_chats": len(unsorted),
               "acted": len(acted), "skipped": len(skipped), "errors": len(errors),
               "followups": followups,
               "details": acted, "skipped_details": skipped, "error_details": errors}
    log(f"=== Итог: обработано {len(acted)}, пропущено {len(skipped)}, ошибок {len(errors)}, дожим {followups} ===")
    print("SUMMARY_JSON:" + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    run()
