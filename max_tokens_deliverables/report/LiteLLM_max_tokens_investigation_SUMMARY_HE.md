# קטיעת `max_tokens` ב-LiteLLM: סיכום החקירה

**היקף:** LiteLLM 1.88.1 (נבדק מול עץ העבודה החי ומול ה-fork).
**תוצאה:** שורש הבעיה אותר, התיקון מומש ונבדק ב-fork שלנו, והוגדרו שלושה מסלולי פתרון משלימים.

---

## 1. הבעיה והסימפטום

לקוחות סוכנים (גם סוכני פורמט OpenAI ב-`/chat/completions` וגם Claude Code בנתיב `/v1/messages` של Anthropic) חוו שני כשלים נפרדים:

- **קטיעה שקטה** — תשובות שנקטעו באמצע עם `finish_reason: "length"` (פורמט OpenAI) או `stop_reason: "max_tokens"` (פורמט Anthropic). הקיצוץ החמיר ככל שהשיחה התארכה, כי הוא גדל עם גודל הפרומפט.
- **כשל קשיח** — חלק מהבקשות נדחו לחלוטין עם `ContextWindowExceeded` או 400 מהספק, לעיתים כשהחשבון נוחת בדיוק על `prompt + max_tokens = context_window + 1`.

**ההוכחה שזה LiteLLM ולא הלקוח או הספק:** עם `--detailed_debug`, ה-proxy מדפיס שורת לוג מילולית `MODIFYING MAX TOKENS` המציגה את `user_max_tokens` המקורי, את `input_tokens` שנספרו ואת `max_output_tokens` של המודל. השוואה בין `max_tokens` שהלקוח שלח לבין ה-payload הגולמי שיצא מה-proxy הראתה שהם שונים.

---

## 2. שורש הבעיה

השכתוב הדינמי נמצא בפונקציה `get_modified_max_tokens` בקובץ `litellm/litellm_core_utils/token_counter.py`. הוא רץ אך ורק כאשר `litellm.modify_params` (משתנה סביבה `LITELLM_MODIFY_PARAMS`) מופעל, נקרא מתוך עוטף ה-`@client` ב-`litellm/utils.py` (בלוקי ה-`CHECK MAX TOKENS`), וחל על סוגי הקריאות `completion`, `acompletion` ו-`anthropic_messages`.

ההיוריסטיקה הפגומה היא **Case 1**:

```python
## CASE 1: model input + output can't exceed X
if _model_info["max_input_tokens"] == max_output_tokens:
    ...
    user_max_tokens = int(max_output_tokens - input_tokens)
```

- השוויון `max_input_tokens == max_output_tokens` משמש כפרוקסי ל-"למודל הזה יש חלון משותף אחד" (נכון למודלי completion ישנים בסגנון gpt-3.5-turbo).
- ואז הוא מחסיר את הפרומפט מתקרת הפלט, פחות buffer דק: `max(0.1 * input_tokens, 10)`, המחושב מהערכת טוקנים מקומית.

**למה זה נכשל:** מאות מודלי chat מודרניים מציגים גבולות קלט/פלט שווים ברישום, אך בפועל יש להם תקציבים עצמאיים (למשל רשומות 256K/256K, וכן הקונפיגורציה שלנו עם 200,000 טוקנים). עבורם LiteLLM מחסיר בטעות את כל הפרומפט מתקרת הפלט. ככל שהשיחה גדלה, `max_tokens` מתכווץ והתשובות נקטעות. ה-buffer מחמיר את המצב: `tiktoken` מעריך בחסר מנגנוני טוקניזציה שאינם של OpenAI ב-15 עד 20 אחוז, יותר מ-buffer של 10 אחוז, ולכן שולי הביטחון אינם מספיקים על פרומפטים של Claude/Llama.

**הערת דיוק (חשובה):** הקטיעה השקטה היא באג מאומת של LiteLLM. הכשל הקשיח `ContextWindowExceeded` מסוג "+1" **אינו** off-by-one של LiteLLM — החשבון הזה מחושב על-ידי הספק עם הטוקנייזר האמיתי שלו ורק מועבר דרך LiteLLM. אין ב-LiteLLM בדיקה מקדימה של `prompt + max_tokens` מול החלון שתכיל off-by-one. תבנית ה-"+1 מעל" היא חתימת אצבע של לקוח שמחשב `max_tokens` שממלא את כל החלון וחורג בטוקן אחד של טעות הערכה. התרומה היחידה האמיתית של LiteLLM למקרה הקשיח היא שה-buffer הנוטה להערכת חסר עלול להשאיר את הערך המשוכתב מעל התקציב בפרומפטים גדולים.

---

## 3. תנאי ההפעלה ורדיוס הפגיעה

- מופעל **רק** כאשר `modify_params: true`.
- כיבוי `modify_params` באופן גלובלי **אינו** מומלץ: הדגל עמוס מדי ושולט גם בהתנהגויות שימושיות באמת (תיקון קריאות-כלי של Anthropic, ניקוי הודעות, הוספת הודעות placeholder/dummy, השמטת פרמטר thinking). בנתיב ה-passthrough המקורי של Claude Code (`/v1/messages`), התנהגויות שינוי-ההודעות אינן רצות, אבל שכתוב ה-max_tokens כן רץ.
- האם השכתוב גורם נזק תלוי בנתוני הרישום (`model_info`) של כל deployment.

---

## 4. אפשרויות הפתרון

### פתרון א' — תיקון רישום המודלים (אפס קוד)
לדרוס `model_info` לכל deployment כך שגבולות הקלט והפלט יהיו ריאליים ושונים, ושוברים את השוויון כך ש-Case 1 לעולם לא יופעל.

```yaml
model_list:
  - model_name: my-model
    litellm_params: { model: ... }
    model_info:
      max_input_tokens: 200000
      max_output_tokens: 4096   # או 64000; רק לא שווה לקלט
```
**שלבים:** (1) לאתר כל מודל שבו `max_input_tokens == max_output_tokens`; (2) להגדיר ערכים נכונים ושונים; (3) לטעון מחדש את ה-proxy ולוודא ש-`MODIFYING MAX TOKENS` נעלם.
**יתרונות:** אפס שינוי קוד; גם משפר ניתוב ומעקב עלויות. **חסרונות:** תלוי במשמעת קונפיגורציה בכל רשומה.

### פתרון ב' — תיקון הקוד ב-upstream (התיקון הארכיטקטוני)
להוסיף תנאי בודד כך שהכיווץ יחול רק על מודלים ישנים אמיתיים:

```python
if (
    _model_info["max_input_tokens"] == max_output_tokens
    and _model_info.get("mode") == "completion"
):
    ...  # רק מודלי חלון-משותף ישנים מתכווצים
```
אומת מול הרישום החי: מודלי completion (למשל `command`) עדיין מתכווצים נכון; מודלי chat עם גבולות שווים (למשל `ai21.jamba-1-5-large-v1:0`) עוברים passthrough. מומש ב-fork שלנו עם בדיקות רגרסיה לשני התרחישים; ruff ו-black נקיים. מסלול ל-PR ב-upstream כדי שנוותר על ה-patch הפרטי.
**הסתייגות ל-reviewers:** `mode == "completion"` הוא פרוקסי היוריסטי, לא ערובה. אם מודל חלון-משותף אמיתי יירשם אי-פעם כ-`chat`, הוא ידלג על הכיווץ ויסתמך על אכיפת הספק — מקובל, שכן הספק אוכף את החלון באופן סמכותי בכל מקרה.

### פתרון ג' — Monkey Patch בזמן ריצה (פתרון ביניים מיידי לפרודקשן)
בעליית ה-proxy, מחליפים את מצביע הפונקציה המטמון ב-passthrough. מנטרל רק את השכתוב; כל שאר התנהגויות `modify_params` נשמרות; שורד `pip install -U litellm`.

```python
# litellm_patch.py  -  נטען פעם אחת בעליית ה-proxy
import litellm._lazy_imports as _li

def _passthrough_max_tokens(*, user_max_tokens=None, **kwargs):
    return user_max_tokens

_li._get_modified_max_tokens_func = _passthrough_max_tokens
```
לטעון אותו דרך `litellm_settings: callbacks: ["litellm_patch"]` (המודול חייב להיות ב-`PYTHONPATH` של ה-proxy). ה-`**kwargs` קולט את כל ששת הארגומנטים שה-wrapper שולח. נקודת ה-patch לגיטימית כי הפונקציה נפתרת דרך lazy-import cache (`litellm/_lazy_imports.py::_get_modified_max_tokens_func`).

---

## 5. השוואת הפתרונות

| פתרון | מאמץ | סיכון | היקף | קביעות |
|---|---|---|---|---|
| א. תיקון רישום | נמוך | נמוך | לכל deployment | תלוי במשמעת |
| ב. תיקון upstream | בינוני | נמוך | גלובלי ונכון | גבוהה; מסיים את ה-fork |
| ג. Monkey patch | נמוך | נמוך מאוד | גלובלי | פתרון ביניים מיידי |

הפתרונות משלימים, לא חלופיים.

---

## 6. המלצה וצעדים הבאים

**הטמעה שכבתית:**
1. **מיידי** — להטמיע את פתרון ג' (passthrough בזמן ריצה) כדי לעצור את הקטיעה בפרודקשן היום.
2. **במקביל** — ליישם את פתרון א'; לתקן `model_info` ל-deployments שלנו, במיוחד לקונפיגורציה של 200,000 טוקנים.
3. **בטווח הקרוב** — למזג את פתרון ב' ל-upstream כדי שהתיקון יהיה קבוע ונסיר את ה-patch הפרטי.

**אימות (לפי מוסכמת הוכחת-התיקון שלנו):** להריץ proxy אמיתי מול ספק אמיתי עם deployment של קלט=פלט, לשלוח `max_tokens` גדול, ולהראות שה-payload לספק נושא את הערך ללא שינוי. לפני: מתעד `MODIFYING MAX TOKENS` עם ערך מכווץ. אחרי: מעביר את ערך הלקוח כמות שהוא.

**פריטי פעולה:**
- להחיל את ה-patch בזמן ריצה על קונפיגורציית הפרודקשן; לוודא ש-`MODIFYING MAX TOKENS` כבר לא מופיע.
- לבדוק את כל רשומות `model_info` לאיתור שוויון קלט/פלט ולתקן אותן.
- לפתוח PR ל-upstream מענף ה-fork עם התיקון המותנה ובדיקות הרגרסיה.
- להוסיף ניטור שמתריע אם שיעור `finish_reason: "length"` עולה שוב.
- להשאיר את `modify_params` פעיל (מומלץ) כדי לשמר את תיקוני ההודעות של Anthropic.

---

## 7. מקורות טכניים

| פריט | מיקום |
|---|---|
| הפונקציה הפגומה | `litellm/litellm_core_utils/token_counter.py::get_modified_max_tokens` |
| שער ה-wrapper | `litellm/utils.py` — בלוקי ה-`CHECK MAX TOKENS` (מותנה ב-`modify_params` ובסוג הקריאה) |
| נקודת ה-monkey-patch | `litellm/_lazy_imports.py::_get_modified_max_tokens_func` |
| מתג אבחון | `litellm_settings: log_raw_request_response: true` |
| גרסה שנבדקה | 1.88.1 |

---

## 8. סיכום השינוי ב-fork (מה שמימשנו בפועל)

- **קוד מקור:** `get_modified_max_tokens` קיבל את התנאי `mode == "completion"` על Case 1 (פתרון ב'). כל הלוגיקה המקורית נשמרה; חתימת הפונקציה לא השתנתה כי עוטף ה-`@client` קורא לה עם כל ששת הארגומנטים בשמות מפתח.
- **בדיקות:** `tests/test_litellm/litellm_core_utils/test_token_counter.py::test_get_modified_max_tokens` הורחב לכיסוי שני התרחישים — מודל completion ישן עדיין מתכווץ (`command, 4000, 256 -> 96`), מודל chat עם גבולות שווים עובר passthrough (`ai21.jamba-1-5-large-v1:0, 250000, 256000 -> 256000`). שורת ה-jamba היא שומר הרגרסיה: היא מחזירה 6000 תחת Case 1 הלא-מותנה הישן, ולכן נכשלת אם התנאי יוסר אי-פעם.
- **ענף:** `litellm_max_tokens_passthrough` (שונה מ-`fix/max-tokens-passthrough` כדי לעמוד במוסכמות השמות של המאגר: בלי לוכסן, עם קידומת `litellm_`).
- **איכות:** קובץ בדיקות token_counter המלא עובר (83 עברו, 1 דולג); ruff נקי; black נקי על השורות שהשתנו.
