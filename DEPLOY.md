# Deployment Guide — دمج الـ KPI System في مشروع PropFinder

## 🎯 الخطة

هنـ replace الملفات القديمة بالنسخة الجديدة على نفس الـ repo. الـ PropFinder هيفضل شغال زي ما هو، والـ KPI system هيتضاف.

---

## 📋 خطوات النشر

### 1. Backup سريع (احتياط)

قبل ما تعمل أي حاجة، خد backup من الـ `app.py` و `index.html` القدامى:

```bash
cp app.py app.py.backup
cp index.html index.html.backup
```

### 2. حط الملفات الجديدة في الـ repo

استخرج الـ `ain-kpi.zip` واعمل **replace كامل** للملفات القديمة:

```
repo/
├── app.py                      ← جديد (يستدعي app/)
├── config.py                   ← جديد
├── requirements.txt            ← محدّث
├── Procfile                    ← محدّث
├── .python-version             ← موجود
├── .gitignore                  ← جديد
├── .env.example                ← جديد
├── README.md                   ← جديد
├── DEPLOY.md                   ← (هذا الملف)
└── app/
    ├── __init__.py
    ├── database.py
    ├── auth.py
    ├── kpi_logic.py
    ├── sync_service.py         ← الـ Master V sync (نفس اللي كان في app.py)
    ├── blueprints/
    │   ├── auth_bp.py
    │   ├── users_bp.py
    │   ├── kpi_bp.py
    │   ├── pages_bp.py
    │   └── propfinder_bp.py    ← /api/units, /api/stats, /api/sync
    ├── static/
    │   ├── css/style.css
    │   └── js/common.js
    └── templates/
        ├── base.html
        ├── login.html
        ├── register.html
        ├── sales.html
        ├── dataentry.html
        ├── dashboard.html
        ├── admin.html
        ├── profile.html
        └── propfinder.html
```

الملفات القديمة اللي يُحذف:
- ❌ `index.html` (القديم في الـ root) — مش محتاجينه
- ❌ `all_units.csv` (لو موجود)
- ❌ `sync_log.txt` (لو موجود)

الملف `app.py` الجديد **صغير جداً** (حوالي 20 سطر) — بينادي على `app/__init__.py` اللي فيه الـ factory.

### 3. ارفع على Railway

```bash
git add .
git commit -m "Add KPI system on top of PropFinder"
git push
```

Railway هيعمل:
1. `pip install -r requirements.txt` — نفس الـ dependencies + إضافات بسيطة
2. `gunicorn app:app` — نفس الـ Procfile command
3. أول ما السيرفر يشتغل، `init_all_tables()` هيعمل:
   - ✅ جدول `users` (جديد) — بيتعمل لو مش موجود
   - ✅ جدول `kpi_entries` (جديد) — بيتعمل لو مش موجود
   - ✅ الـ admin الافتراضي (`admin` / `admin123`)
   - ❌ **مش بيلمس** جدول `units` — هيفضل زي ما هو بكل الـ data

### 4. متغيرات البيئة في Railway (Variables)

اللي موجودة عندك حالياً:
```
DATABASE_URL=postgresql://postgres:AdPVLYioZHOYsrpSswoILIvpkHwIReTz@caboose.proxy.rlwy.net:21778/railway
DISABLE_SYNC=true
PORT=5000
```

دول كافيين 100%. لو عايز تضيف حاجة:

**مستحسن تضيفها:**
```
SECRET_KEY=<random-64-char-string>
DEFAULT_ADMIN_PASSWORD=<strong-password>
```

**ليه؟**
- `SECRET_KEY`: لو مش موجود، الـ sessions هتـ reset عند كل restart
- `DEFAULT_ADMIN_PASSWORD`: عشان تحط باسورد قوي من البداية بدل `admin123`

لتوليد SECRET_KEY قوي:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## ✅ التحقق من النشر

بعد الـ deploy، افتح الروابط دي بالترتيب:

| الرابط | المفروض يحصل |
|--------|---------------|
| `/` | redirect إلى `/login` |
| `/login` | صفحة تسجيل دخول |
| Login بـ `admin` / `admin123` | redirect إلى `/admin` |
| `/admin` | قائمة بمستخدم واحد (الـ admin) |
| `/propfinder` | صفحة فيها الـ units القديمة |

---

## 🆘 لو حصلت مشكلة

### خطأ: `ModuleNotFoundError: No module named 'app'`
تأكد إن فولدر `app/` موجود في نفس مستوى `app.py` ومعاه `__init__.py`.

### خطأ: `connection refused` للـ DB
في Railway، الـ `DATABASE_URL` بيتحط تلقائي. بس لو بتشغل locally، استخدم الـ proxy URL الـ external.

### الـ PropFinder مش بيعرض units
طبيعي لو `DISABLE_SYNC=true` — السيرفر مش بيعمل sync. لتشغيل sync يدوي:
1. غيّر `DISABLE_SYNC` إلى `false` في Railway
2. افتح `/propfinder` واضغط "تشغيل Sync يدوياً"
3. ارجع `DISABLE_SYNC=true` بعد ما يخلص

### مش قادر تسجل دخول
- تأكد إن الـ DB connection شغالة (افتح `/api/health`)
- امسح الـ cookies من المتصفح وجرب تاني
- لو الـ default admin مش متعمل، اعمل reset لجدول users يدوياً:
  ```sql
  TRUNCATE users CASCADE;
  ```
  وبعدين restart السيرفر على Railway

### الجلسة بتنتهي بسرعة
لو `SECRET_KEY` مش متعيّن في Railway، الجلسات بتـ reset عند كل restart.
حل: ضيف `SECRET_KEY` في Variables.

---

## 📊 الـ Routes الكاملة

### صفحات HTML (بعد تسجيل الدخول)
- `/` → redirect لصفحة الـ role
- `/sales` — Sales يسجل بياناته
- `/data-entry` — Data Entry / Manager يقيّم
- `/dashboard` — Manager يشوف التقارير
- `/admin` — Admin يدير المستخدمين
- `/profile` — تغيير كلمة المرور
- `/propfinder` — الصفحة القديمة (units)

### API
- `POST /api/auth/login` — تسجيل دخول
- `POST /api/auth/register` — تسجيل حساب Sales
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `POST /api/auth/change-password`
- `GET /api/users` — قائمة المستخدمين (admin/manager)
- `POST /api/users` — إضافة (admin)
- `PUT /api/users/:id` — تعديل (admin)
- `DELETE /api/users/:id` — حذف (admin)
- `POST /api/users/:id/activate` | `/deactivate`
- `GET /api/kpi/config` — إعدادات الـ KPIs
- `GET /api/kpi/months` — الشهور المتاحة
- `POST /api/kpi/submit/sales` — Sales يسجل
- `POST /api/kpi/submit/evaluation` — Data Entry يقيّم
- `GET /api/kpi/entry/:user_id/:month`
- `GET /api/kpi/report?month=YYYY-MM`
- `GET /api/kpi/summary?month=YYYY-MM`
- `DELETE /api/kpi/entry/:id`
- `GET /api/units` — PropFinder (existing)
- `GET /api/stats` — PropFinder (existing)
- `GET /api/sync/status` — PropFinder sync
- `POST /api/sync/trigger` — PropFinder sync
- `GET /api/health` — health check (مش محتاج login)

---

## 🎨 الـ KPIs ومعادلة الحساب

| KPI | Weight | الهدف | من يملأ |
|-----|--------|-------|---------|
| Calls | 15 | 2000/شهر | Sales |
| Meetings | 8 | 20% من Leads | Sales |
| CRM | 10 | 95% | Sales |
| Deals | 10 | 3% من Leads | Sales |
| Reports | 8 | 4/شهر | Sales |
| Reservations | 7 | 7% من Leads | Sales |
| Follow-up | 15 | 100% | Sales |
| Attendance | 7 | 100% | Sales |
| Attitude | 4 | Pass/Fail | Manager |
| Presentation | 4 | Pass/Fail | Manager |
| Behaviour | 4 | Pass/Fail | Manager |
| Appearance | 4 | Pass/Fail | Manager |
| HR Roles | 4 | Pass/Fail | Manager |
| **Total** | **100** | | |

المعادلة:
```
Achievement = MIN(Actual / Target, 1)
Weighted Score = Achievement × Weight
Total KPI % = SUM(all Weighted Scores)
```

Rating:
- ≥90 → Excellent
- ≥75 → V.Good
- ≥55 → Good
- ≥40 → Medium
- ≥25 → Weak
- <25 → Bad
