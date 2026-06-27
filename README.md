# Ain Real Estate — KPI & Sales Intelligence System

نظام متكامل لإدارة أداء فريق المبيعات في شركة Ain Real Estate.
يجمع بين **KPI tracking** للـ Sales + **PropFinder** لعرض العقارات من Master V.

---

## المميزات

- **نظام Role-based** بأربعة أدوار:
  - `Admin` — تحكم كامل + إدارة المستخدمين
  - `Manager` — يشوف كل التقارير ويقيّم الفريق
  - `Data Entry` — يملأ تقييم المدير للفريق
  - `Sales` — يسجل بياناته فقط

- **تسجيل دخول وخروج** مع sessions
- **تسجيل حساب ذاتي** للـ Sales
- **لوحة مدير** مع Breakdown كامل لكل KPI
- **إدارة المستخدمين** CRUD كامل
- **PropFinder** مزامنة تلقائية من Master V API كل 14 يوم

---

## Project Structure

```
ain-kpi/
├── app.py                      ← Entry point
├── config.py                   ← إعدادات البيئة
├── requirements.txt            ← Python dependencies
├── Procfile                    ← Railway startup command
├── .python-version             ← Python 3.11
├── .gitignore                  ← Files Git يتجاهلها
├── .env.example                ← متغيرات البيئة المطلوبة
├── README.md                   ← هذا الملف
└── app/
    ├── __init__.py             ← Flask app factory
    ├── database.py             ← DB connection + schema init
    ├── auth.py                 ← Auth helpers + decorators
    ├── kpi_logic.py            ← KPI config + scoring formula
    ├── sync_service.py         ← Master V sync (background)
    │
    ├── blueprints/             ← Route handlers (منفصلة حسب المسؤولية)
    │   ├── __init__.py
    │   ├── auth_bp.py          ← /api/auth/* (login, logout, register)
    │   ├── users_bp.py         ← /api/users/* (user CRUD - Admin only)
    │   ├── kpi_bp.py           ← /api/kpi/* (KPI entries + reports)
    │   ├── pages_bp.py         ← HTML pages routing
    │   └── propfinder_bp.py    ← /api/units, /api/stats, /api/sync
    │
    ├── static/
    │   ├── css/style.css       ← Design system موحد
    │   └── js/common.js        ← JS utilities (API, toast, modals)
    │
    └── templates/
        ├── base.html           ← Layout مشترك مع Topnav
        ├── login.html
        ├── register.html
        ├── sales.html          ← صفحة الـ Sales (role: sales)
        ├── dataentry.html      ← صفحة التقييم (role: dataentry+)
        ├── dashboard.html      ← لوحة المدير (role: manager+)
        ├── admin.html          ← إدارة المستخدمين (role: admin)
        ├── profile.html        ← ملف المستخدم الشخصي
        └── propfinder.html     ← عرض العقارات
```

---

## التشغيل المحلي

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd ain-kpi

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy env example and edit
cp .env.example .env
# حدّث DATABASE_URL والـ SECRET_KEY

# 4. Run
python app.py
# أو
gunicorn app:app
```

افتح `http://localhost:8080/login` في المتصفح.

---

## النشر على Railway

```bash
# 1. أضف الملفات المُحدَّثة
git add .
git commit -m "Deploy Ain KPI System"
git push
```

Railway هيعمل:
1. pip install تلقائي
2. تنفيذ الأمر في الـ Procfile
3. الأعمدة والجداول تُنشأ تلقائياً عند أول تشغيل (`init_all_tables()`)
4. **Admin افتراضي يُنشأ إذا لم يوجد أي user:**
   - Username: `admin`
   - Password: `admin123`
   - **⚠️ غيّر كلمة المرور فوراً من `/profile` بعد أول دخول!**

---

## استخدام النظام

### أول مرة:

1. ادخل بحساب الـ admin الافتراضي: `admin` / `admin123`
2. روح `/admin` وأضف المستخدمين:
   - كل Sales بدور `sales`
   - المدير بدور `manager`
   - مسؤول إدخال البيانات بدور `dataentry`
3. غير كلمة المرور للـ admin من `/profile`

### لكل شهر:

1. **الـ Sales** يدخل عبر `/login` ويروح `/sales` يسجل بياناته
2. **الـ Data Entry / Manager** يروح `/data-entry` ويقيّم كل Sales
3. **الـ Manager** يروح `/dashboard` يشوف التقارير و Rankings

---

## الـ KPIs ومعادلة الحساب

| KPI | Weight | الهدف | من يملأ |
|-----|--------|-------|---------|
| Calls | 15 | 2000/شهر | Sales |
| Meetings | 8 | 20% من Fresh Leads | Sales |
| CRM Update | 10 | 95% | Sales |
| Deals | 10 | 3% من Fresh Leads | Sales |
| Reports | 8 | 4/شهر | Sales |
| Reservations | 7 | 7% من Fresh Leads | Sales |
| Follow-up | 15 | 100% | Sales |
| Attendance | 7 | 100% | Sales |
| Attitude | 4 | 100% (Pass/Fail) | Data Entry |
| Presentation | 4 | 100% (Pass/Fail) | Data Entry |
| Behaviour | 4 | 100% (Pass/Fail) | Data Entry |
| Appearance | 4 | 100% (Pass/Fail) | Data Entry |
| HR Roles | 4 | 100% (Pass/Fail) | Data Entry |
| **Total** | **100** | | |

### المعادلة:

```
Achievement = MIN(Actual / Target, 1)
Weighted Score = Achievement × Weight
Total KPI % = SUM(all Weighted Scores)

Rating:
  ≥90 → Excellent
  ≥75 → V.Good
  ≥55 → Good
  ≥40 → Medium
  ≥25 → Weak
  < 25 → Bad
```

---

## API Reference

كل الـ endpoints تحت `/api/*` وتتطلب تسجيل دخول (عدا `/api/auth/login` و `/api/auth/register`).

### Auth
- `POST /api/auth/login` — تسجيل دخول
- `POST /api/auth/register` — إنشاء حساب Sales
- `POST /api/auth/logout` — تسجيل خروج
- `GET /api/auth/me` — بيانات المستخدم الحالي
- `POST /api/auth/change-password` — تغيير كلمة المرور

### Users (Admin only)
- `GET /api/users` — قائمة المستخدمين
- `POST /api/users` — إضافة مستخدم
- `PUT /api/users/<id>` — تعديل
- `DELETE /api/users/<id>` — حذف
- `POST /api/users/<id>/activate` — تفعيل
- `POST /api/users/<id>/deactivate` — تعطيل

### KPI
- `GET /api/kpi/config` — إعدادات الـ KPIs (weights, targets)
- `GET /api/kpi/months` — الشهور المتاحة
- `POST /api/kpi/submit/sales` — Sales يسجل بياناته
- `POST /api/kpi/submit/evaluation` — Data Entry يقيّم
- `GET /api/kpi/entry/<user_id>/<month>` — بيانات شهر محدد
- `GET /api/kpi/report?month=YYYY-MM` — تقرير شامل
- `GET /api/kpi/summary?month=YYYY-MM` — ملخص الشهر
- `DELETE /api/kpi/entry/<id>` — حذف سجل

### PropFinder
- `GET /api/units` — كل الـ units
- `GET /api/stats` — إحصائيات
- `GET /api/sync/status` — حالة الـ sync
- `POST /api/sync/trigger` — تشغيل sync يدوي

---

## Tech Stack

- **Backend:** Flask + Gunicorn
- **Database:** PostgreSQL (psycopg2)
- **Auth:** Flask sessions + SHA-256 password hashing
- **Frontend:** Vanilla JS + Jinja2 templates + Unified CSS
- **Hosting:** Railway

---

## Support

لأي مشكلة، راجع الـ logs في Railway أو افتح issue في الـ repo.
