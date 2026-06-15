# הטמעת ה-max_tokens passthrough patch ב-Deploy

שני מסלולים להחלת ה-monkey patch שמבטל את שכתוב ה-`max_tokens` השגוי
(`get_modified_max_tokens` / Case 1) בלי לכבות `modify_params` ובלי לגעת בשום
התנהגות אחרת. בחרי אחד מהם.

קבצים בתיקייה:
- `sitecustomize.py` — קוד ה-patch (גרסת פרודקשן, תמיד פעיל)
- `Dockerfile` — מסלול א': image נגזר
- `configmap.yaml` + `values-patch.yaml` — מסלול ב': Helm בלי build

איך זה עובד: CPython טוען אוטומטית מודול בשם `sitecustomize` בעליית האינטרפרטר,
אם התיקייה שלו נמצאת ב-`PYTHONPATH`. ה-patch מחליף את מצביע הפונקציה המטמון
`litellm._lazy_imports._get_modified_max_tokens_func` ב-passthrough. `PYTHONPATH`
נוסף *בראש* ל-`sys.path`, ולכן אינו מסתיר את חבילת `litellm` המותקנת.

---

## מסלול א' — Dockerfile (image נגזר, immutable)

מתי: כשהמדיניות אוסרת קוד ב-ConfigMap, או כשרוצים artifact אחד חתום ומבוקר.

```bash
cd deploy
docker build -t <your-registry>/litellm-patched:v1.89.0-maxtokens .
docker push <your-registry>/litellm-patched:v1.89.0-maxtokens
```

ואז ב-`values.yaml` של ה-chart:

```yaml
image:
  repository: <your-registry>/litellm-patched
  tag: v1.89.0-maxtokens
```

```bash
helm upgrade <release> ./deploy/charts/litellm-helm -n <ns> -f my-values.yaml
```

חיסרון: צריך לחתוך image מחדש בכל שדרוג גרסה של LiteLLM. הצמדנו את ה-`FROM`
לתג מדויק בכוונה.

---

## מסלול ב' — Helm בלי build (ConfigMap + mount), מומלץ

מתי: רוצים לשרוד שדרוגי `image.tag` בלי לבנות כלום. ה-patch הוא קובץ נתונים,
לא חלק מה-image.

```bash
# 1. ליצור את ה-ConfigMap עם ה-patch (אותו namespace של ה-release)
kubectl apply -n <ns> -f configmap.yaml

# 2. לשדרג את ה-release עם ה-overlay שמרכיב את ה-mount + PYTHONPATH
helm upgrade <release> ./deploy/charts/litellm-helm -n <ns> \
  -f my-values.yaml -f values-patch.yaml
```

`values-patch.yaml` מוסיף שלושה דברים בלבד: `envVars.PYTHONPATH=/patch`, volume
מסוג configMap, ו-volumeMount ל-`/patch`. ה-ConfigMap עולה כתיקייה, כך
ש-`sitecustomize.py` יושב ב-`/patch/sitecustomize.py`.

עדכון עתידי של ה-patch: לערוך את `configmap.yaml`, `kubectl apply` שוב, ואז
`kubectl rollout restart deploy/<release>-litellm` כדי לטעון מחדש.

---

## אימות אחרי rollout

```bash
# 1. פונקציית השכתוב היא עכשיו ה-passthrough שלנו (לא ברירת המחדל):
kubectl exec -n <ns> deploy/<release>-litellm -- \
  python -c "import litellm._lazy_imports as l; print(l._get_modified_max_tokens_func)"
# מצופה: <function _passthrough_max_tokens ...>

# 2. שורת השכתוב לא אמורה להופיע יותר בלוגים:
kubectl logs -n <ns> deploy/<release>-litellm | grep -c "MODIFYING MAX TOKENS"   # 0

# 3. (אופציונלי) מרקר הטעינה של ה-patch:
kubectl logs -n <ns> deploy/<release>-litellm | grep "litellm-patch"
```

---

## אזהרות

- `sitecustomize` נטען בכל תהליך Python ב-pod (גם worker-ים של ה-proxy וגם
  shells של מיגרציית prisma). כאן זה לא מזיק (ה-patch idempotent וזעיר). אם
  רוצים scope רק לתהליך ה-proxy, אפשר לטעון דרך `litellm_settings: callbacks`
  בקונפיג במקום `sitecustomize` — אותו ConfigMap, hook אחר.
- ה-patch נשען על סמל פנימי (`_get_modified_max_tokens_func`). בכל שדרוג מייג'ור
  של LiteLLM יש להריץ שוב את בדיקת האימות (1) למעלה. אם הסמל ישונה אי-פעם,
  ה-patch הופך ל-no-op שקט (לא יקרוס, פשוט יפסיק לפעול). זו השבריריות המובנית
  של כל monkey patch, ולכן התיקון ב-upstream (`mode == "completion"`) הוא הפתרון
  הקבוע; ה-patch הוא פתרון ביניים מיידי.
- `PYTHONPATH` מצביע על `/patch` בלבד. אם ה-image שלך כבר מגדיר `PYTHONPATH`
  משלו (ברירת המחדל של LiteLLM לא), שרשרי במקום לדרוס.
