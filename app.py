#!/usr/bin/env python3
"""
Company Resources — Ажилтан, тоног төхөөрөмж, гэрээний нөөцийн санг удирдах систем
Tender-dashboard-ын PostgreSQL-тэй холбогдоно.
"""

import os
import io
import re
import json
import uuid
import shutil
import hashlib
import secrets
import threading
import time
from pathlib import Path
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, session, send_file, send_from_directory, redirect
import psycopg2
import psycopg2.extras
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

BASE_DIR = Path(__file__).parent
UPLOAD_ROOT = BASE_DIR / "uploads"
UPLOAD_ROOT.mkdir(exist_ok=True)
for sub in ("employees", "equipment", "contracts"):
    (UPLOAD_ROOT / sub).mkdir(exist_ok=True)
INBOX_ROOT = UPLOAD_ROOT / "employees" / "inbox"
INBOX_ROOT.mkdir(exist_ok=True)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("DB_NAME", "tender_db"),
    "user": os.environ.get("DB_USER", "tender_admin"),
    "password": os.environ.get("DB_PASSWORD", "admin_pass"),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


def query(sql, params=None, fetchone=False, fetchall=False, commit=False):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        result = None
        if fetchone:
            result = cur.fetchone()
        elif fetchall:
            result = cur.fetchall()
        if commit:
            conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Ажилтны анкет
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id SERIAL PRIMARY KEY,
            register_no TEXT UNIQUE,
            last_name TEXT,
            first_name TEXT,
            birth_date DATE,
            gender TEXT,
            position TEXT,
            department TEXT,
            education TEXT,
            profession TEXT,
            work_start_date DATE,
            experience_years NUMERIC(4,1) DEFAULT 0,
            phone TEXT,
            email TEXT,
            address TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Тоног төхөөрөмж
    cur.execute("""
        CREATE TABLE IF NOT EXISTS equipment (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            model TEXT,
            serial_no TEXT,
            manufactured_year INTEGER,
            country TEXT,
            quantity INTEGER DEFAULT 1,
            unit TEXT DEFAULT 'ш',
            capacity TEXT,
            condition TEXT DEFAULT 'Сайн',
            purchase_date DATE,
            purchase_price NUMERIC(18,2),
            current_value NUMERIC(18,2),
            location TEXT,
            certificate_no TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Ижил төстэй гэрээнүүд
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            id SERIAL PRIMARY KEY,
            contract_no TEXT,
            name TEXT NOT NULL,
            customer TEXT,
            contract_type TEXT,
            start_date DATE,
            end_date DATE,
            amount NUMERIC(18,2),
            currency TEXT DEFAULT 'MNT',
            status TEXT DEFAULT 'Гүйцэтгэж буй',
            description TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Бүх төрлийн баримт бичгүүд
    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            doc_type TEXT,
            title TEXT,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            file_size INTEGER,
            mime_type TEXT,
            uploaded_by INTEGER,
            uploaded_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_entity ON documents(entity_type, entity_id)")

    conn.commit()
    conn.close()


# ============================================================
# AUTH (tender_app-тай ижил users хүснэгт)
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Нэвтрэх шаардлагатай"}), 401
        return f(*args, **kwargs)
    return decorated


@app.route("/api/login", methods=["POST"])
def login():
    from werkzeug.security import check_password_hash
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    user = query("SELECT id, username, full_name, role, password_hash FROM users WHERE username=%s",
                 (username,), fetchone=True)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Нэр эсвэл нууц үг буруу"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["full_name"] = user["full_name"]
    session["role"] = user["role"]
    return jsonify({"success": True, "user": {"username": user["username"], "full_name": user["full_name"], "role": user["role"]}})


@app.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False}), 401
    return jsonify({"logged_in": True, "user": {"username": session["username"], "full_name": session["full_name"], "role": session["role"]}})


@app.route("/sso")
def sso_login():
    token = request.args.get("token", "")
    if not token:
        return redirect("/")
    row = query("""
        SELECT * FROM sso_tokens
        WHERE token = %s AND expires_at > NOW()
    """, (token,), fetchone=True)
    if not row:
        return redirect("/")
    session["user_id"]  = row["user_id"]
    session["username"] = row["username"]
    session["full_name"]= row["full_name"]
    session["role"]     = row["role"]
    query("DELETE FROM sso_tokens WHERE token = %s", (token,), commit=True)
    return redirect("/")


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# ============================================================
# ЕРӨНХИЙ CRUD HELPER
# ============================================================

def serialize(row):
    from decimal import Decimal
    if not row:
        return row
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = float(v)
        elif isinstance(v, (datetime,)):
            result[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


# ============================================================
# АЖИЛТАН
# ============================================================

EMP_FIELDS = ["register_no", "last_name", "first_name", "birth_date", "gender",
              "position", "department", "education", "profession",
              "work_start_date", "experience_years", "phone", "email", "address", "notes"]


@app.route("/api/employees")
@login_required
def list_employees():
    search = request.args.get("search", "")
    dept = request.args.get("department", "")
    where = ["1=1"]
    params = []
    if search:
        where.append("(last_name ILIKE %s OR first_name ILIKE %s OR position ILIKE %s OR register_no ILIKE %s)")
        params.extend([f"%{search}%"] * 4)
    if dept:
        where.append("department = %s")
        params.append(dept)
    rows = query(f"SELECT * FROM employees WHERE {' AND '.join(where)} ORDER BY last_name, first_name", params, fetchall=True)
    return jsonify([serialize(r) for r in rows])


@app.route("/api/employees/<int:eid>")
@login_required
def get_employee(eid):
    row = query("SELECT * FROM employees WHERE id=%s", (eid,), fetchone=True)
    if not row:
        return jsonify({"error": "Олдсонгүй"}), 404
    docs = query("SELECT * FROM documents WHERE entity_type='employee' AND entity_id=%s ORDER BY uploaded_at DESC", (eid,), fetchall=True)
    return jsonify({**serialize(row), "documents": [serialize(d) for d in docs]})


@app.route("/api/employees", methods=["POST"])
@login_required
def create_employee():
    data = request.get_json()
    vals = [data.get(f) or None for f in EMP_FIELDS]
    cols = ", ".join(EMP_FIELDS)
    placeholders = ", ".join(["%s"] * len(EMP_FIELDS))
    row = query(f"INSERT INTO employees ({cols}) VALUES ({placeholders}) RETURNING *",
                vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/employees/<int:eid>", methods=["PUT"])
@login_required
def update_employee(eid):
    data = request.get_json()
    sets = ", ".join([f"{f}=%s" for f in EMP_FIELDS]) + ", updated_at=NOW()"
    vals = [data.get(f) or None for f in EMP_FIELDS] + [eid]
    row = query(f"UPDATE employees SET {sets} WHERE id=%s RETURNING *", vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/employees/<int:eid>", methods=["DELETE"])
@login_required
def delete_employee(eid):
    query("DELETE FROM employees WHERE id=%s", (eid,), commit=True)
    return jsonify({"success": True})


# ============================================================
# ТОНОГ ТӨХӨӨРӨМЖ
# ============================================================

EQ_FIELDS = ["name", "category", "model", "serial_no", "manufactured_year", "country",
             "quantity", "unit", "capacity", "condition", "purchase_date",
             "purchase_price", "current_value", "location", "certificate_no", "notes"]


@app.route("/api/equipment")
@login_required
def list_equipment():
    search = request.args.get("search", "")
    category = request.args.get("category", "")
    where = ["1=1"]
    params = []
    if search:
        where.append("(name ILIKE %s OR model ILIKE %s OR serial_no ILIKE %s)")
        params.extend([f"%{search}%"] * 3)
    if category:
        where.append("category = %s")
        params.append(category)
    rows = query(f"SELECT * FROM equipment WHERE {' AND '.join(where)} ORDER BY name", params, fetchall=True)
    return jsonify([serialize(r) for r in rows])


@app.route("/api/equipment/<int:eid>")
@login_required
def get_equipment(eid):
    row = query("SELECT * FROM equipment WHERE id=%s", (eid,), fetchone=True)
    if not row:
        return jsonify({"error": "Олдсонгүй"}), 404
    docs = query("SELECT * FROM documents WHERE entity_type='equipment' AND entity_id=%s ORDER BY uploaded_at DESC", (eid,), fetchall=True)
    return jsonify({**serialize(row), "documents": [serialize(d) for d in docs]})


@app.route("/api/equipment", methods=["POST"])
@login_required
def create_equipment():
    data = request.get_json()
    vals = [data.get(f) or None for f in EQ_FIELDS]
    cols = ", ".join(EQ_FIELDS)
    placeholders = ", ".join(["%s"] * len(EQ_FIELDS))
    row = query(f"INSERT INTO equipment ({cols}) VALUES ({placeholders}) RETURNING *",
                vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/equipment/<int:eid>", methods=["PUT"])
@login_required
def update_equipment(eid):
    data = request.get_json()
    sets = ", ".join([f"{f}=%s" for f in EQ_FIELDS]) + ", updated_at=NOW()"
    vals = [data.get(f) or None for f in EQ_FIELDS] + [eid]
    row = query(f"UPDATE equipment SET {sets} WHERE id=%s RETURNING *", vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/equipment/<int:eid>", methods=["DELETE"])
@login_required
def delete_equipment(eid):
    query("DELETE FROM equipment WHERE id=%s", (eid,), commit=True)
    return jsonify({"success": True})


# ============================================================
# ГЭРЭЭ
# ============================================================

CT_FIELDS = ["contract_no", "name", "customer", "contract_type", "start_date",
             "end_date", "amount", "currency", "status", "description", "notes"]


@app.route("/api/contracts")
@login_required
def list_contracts():
    search = request.args.get("search", "")
    status = request.args.get("status", "")
    where = ["1=1"]
    params = []
    if search:
        where.append("(name ILIKE %s OR customer ILIKE %s OR contract_no ILIKE %s)")
        params.extend([f"%{search}%"] * 3)
    if status:
        where.append("status = %s")
        params.append(status)
    rows = query(f"SELECT * FROM contracts WHERE {' AND '.join(where)} ORDER BY start_date DESC NULLS LAST", params, fetchall=True)
    return jsonify([serialize(r) for r in rows])


@app.route("/api/contracts/<int:cid>")
@login_required
def get_contract(cid):
    row = query("SELECT * FROM contracts WHERE id=%s", (cid,), fetchone=True)
    if not row:
        return jsonify({"error": "Олдсонгүй"}), 404
    docs = query("SELECT * FROM documents WHERE entity_type='contract' AND entity_id=%s ORDER BY uploaded_at DESC", (cid,), fetchall=True)
    return jsonify({**serialize(row), "documents": [serialize(d) for d in docs]})


@app.route("/api/contracts", methods=["POST"])
@login_required
def create_contract():
    data = request.get_json()
    vals = [data.get(f) or None for f in CT_FIELDS]
    cols = ", ".join(CT_FIELDS)
    placeholders = ", ".join(["%s"] * len(CT_FIELDS))
    row = query(f"INSERT INTO contracts ({cols}) VALUES ({placeholders}) RETURNING *",
                vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/contracts/<int:cid>", methods=["PUT"])
@login_required
def update_contract(cid):
    data = request.get_json()
    sets = ", ".join([f"{f}=%s" for f in CT_FIELDS]) + ", updated_at=NOW()"
    vals = [data.get(f) or None for f in CT_FIELDS] + [cid]
    row = query(f"UPDATE contracts SET {sets} WHERE id=%s RETURNING *", vals, fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/contracts/<int:cid>", methods=["DELETE"])
@login_required
def delete_contract(cid):
    query("DELETE FROM contracts WHERE id=%s", (cid,), commit=True)
    return jsonify({"success": True})


# ============================================================
# INBOX АВТОМАТ УНШИГЧ
# ============================================================

DOC_TYPE_KEYWORDS = {
    "CV":              ["cv", "анкет", "resume"],
    "Диплом":          ["диплом", "diplom", "degree"],
    "Иргэний үнэмлэх": ["үнэмлэх", "иргэн", "id", "passport"],
    "НДШ лавлагаа":    ["ндш", "ndsh", "даатгал", "insurance"],
    "Гэрчилгээ":       ["гэрчилгээ", "cert", "certificate"],
}


def detect_doc_type(filename: str) -> str:
    lower = filename.lower()
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return doc_type
    return "Бусад"


def employee_folder_name(emp) -> str:
    """Б.Болд хэлбэрийн хавтасны нэр"""
    last = (emp.get("last_name") or "").strip()
    first = (emp.get("first_name") or "").strip()
    initial = last[0].upper() if last else "?"
    return f"{initial}.{first}"


def parse_inbox_folder(folder_name: str):
    """
    Хавтасны нэрнээс (initial, first_name) гаргана.
    Дэмждэг форматууд:
      "Б.Болд"  "1. Г. Амарсайхан"  "14. Г.Галхүү ШААРДЛАГАГҮЙ"
      "59.О. Отгонсүрэн"  "63. Б. Дашмягмар"  "15. А.Ган-Эрдэнэ"
    """
    # Эхний тоо + цэг + зай-г арилгана: "10. " | "59." → ""
    name = re.sub(r'^\d+\.\s*', '', folder_name).strip()
    # Үлдсэн хэсгээс:  "Б.Болд"  "Г. Амарсайхан"  "Г.Галхүү ШААРДЛАГАГҮЙ"
    m = re.match(r'^([А-ЯӨҮЁа-яөүё])\.\s*([А-ЯӨҮЁа-яөүё][А-ЯӨҮЁа-яөүё\-]*)', name)
    if not m:
        return None, None
    return m.group(1).upper(), m.group(2)


def scan_inbox() -> dict:
    """Inbox хавтас бүрийг шалгаж шинэ файл олдвол DB-д бүртгэнэ"""
    if not INBOX_ROOT.exists():
        return {"imported": 0, "skipped": 0, "errors": []}

    imported = skipped = 0
    errors = []

    for folder in sorted(INBOX_ROOT.iterdir()):
        if not folder.is_dir():
            continue

        initial, first_name = parse_inbox_folder(folder.name)
        if not initial or not first_name:
            errors.append(f"Формат таарахгүй: {folder.name}")
            continue

        emp = query(
            "SELECT * FROM employees WHERE UPPER(LEFT(last_name,1))=%s AND UPPER(first_name)=UPPER(%s)",
            (initial, first_name), fetchone=True
        )
        if not emp:
            errors.append(f"Ажилтан олдсонгүй: {folder.name} ({initial}.{first_name})")
            continue

        for file_path in sorted(folder.iterdir()):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in ALLOWED:
                continue

            existing = query(
                "SELECT id FROM documents WHERE entity_type='employee' AND entity_id=%s AND filename=%s",
                (emp["id"], file_path.name), fetchone=True
            )
            if existing:
                skipped += 1
                continue

            doc_type = detect_doc_type(file_path.name)
            stored_name = f"{uuid.uuid4().hex}_{secure_filename(file_path.name)}"
            dest = UPLOAD_ROOT / "employees" / stored_name
            shutil.copy2(str(file_path), str(dest))
            size = dest.stat().st_size

            query("""
                INSERT INTO documents
                    (entity_type, entity_id, doc_type, title, filename, stored_name, file_size, mime_type, uploaded_by)
                VALUES ('employee', %s, %s, %s, %s, %s, %s, %s, 1)
            """, (emp["id"], doc_type, file_path.name, file_path.name,
                  stored_name, size, "application/octet-stream"), commit=True)
            imported += 1

    return {"imported": imported, "skipped": skipped, "errors": errors}


def _inbox_watcher():
    """2 минут тутамд inbox шалгах background thread"""
    while True:
        try:
            result = scan_inbox()
            if result["imported"] > 0:
                print(f"[inbox] Автомат: +{result['imported']} файл бүртгэгдлэа", flush=True)
        except Exception as e:
            print(f"[inbox] Алдаа: {e}", flush=True)
        time.sleep(120)


@app.route("/api/employees/scan-inbox", methods=["POST"])
@login_required
def api_scan_inbox():
    result = scan_inbox()
    return jsonify(result)


@app.route("/api/employees/scan-preview")
@login_required
def scan_preview():
    """Inbox хавтас бүрийг parse хийж DB-тэй тулгасан үр дүнг буцаана (файл хөдөлгөхгүй)"""
    if not INBOX_ROOT.exists():
        return jsonify([])
    rows = []
    for folder in sorted(INBOX_ROOT.iterdir()):
        if not folder.is_dir():
            continue
        initial, first_name = parse_inbox_folder(folder.name)
        if not initial or not first_name:
            rows.append({"folder": folder.name, "status": "format_error", "employee": None})
            continue
        emp = query(
            "SELECT id, last_name, first_name FROM employees WHERE UPPER(LEFT(last_name,1))=%s AND UPPER(first_name)=UPPER(%s)",
            (initial, first_name), fetchone=True
        )
        files = [f.name for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED]
        rows.append({
            "folder": folder.name,
            "parsed": f"{initial}.{first_name}",
            "status": "matched" if emp else "not_found",
            "employee": serialize(emp) if emp else None,
            "files": files,
        })
    return jsonify(rows)


@app.route("/api/employees/inbox-folders")
@login_required
def inbox_folders():
    """Бүх ажилтны inbox хавтасны нэр, замыг буцаана"""
    employees = query("SELECT id, last_name, first_name FROM employees ORDER BY last_name", fetchall=True)
    result = []
    for e in employees:
        folder_name = employee_folder_name(e)
        folder_path = INBOX_ROOT / folder_name
        folder_path.mkdir(exist_ok=True)
        result.append({
            "employee_id": e["id"],
            "folder_name": folder_name,
            "exists": folder_path.is_dir(),
        })
    return jsonify(result)


# ============================================================
# БАРИМТ БИЧИГ UPLOAD
# ============================================================

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx", ".doc", ".xls"}


@app.route("/api/documents/upload", methods=["POST"])
@login_required
def upload_document():
    entity_type = request.form.get("entity_type", "")
    entity_id = request.form.get("entity_id", "")
    doc_type = request.form.get("doc_type", "")
    title = request.form.get("title", "")

    if entity_type not in ("employee", "equipment", "contract"):
        return jsonify({"error": "entity_type буруу"}), 400
    if not entity_id:
        return jsonify({"error": "entity_id шаардлагатай"}), 400

    if "file" not in request.files:
        return jsonify({"error": "Файл сонгоно уу"}), 400
    f = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify({"error": f"Зөвшөөрөгдөөгүй өргөтгөл: {ext}"}), 400

    safe_name = secure_filename(f.filename)
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    folder = UPLOAD_ROOT / f"{entity_type}s"
    folder.mkdir(exist_ok=True)
    path = folder / stored_name
    f.save(str(path))
    size = path.stat().st_size

    row = query("""
        INSERT INTO documents (entity_type, entity_id, doc_type, title, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """, (entity_type, int(entity_id), doc_type, title or safe_name, f.filename, stored_name, size, f.mimetype, session["user_id"]),
    fetchone=True, commit=True)
    return jsonify(serialize(row))


@app.route("/api/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    doc = query("SELECT * FROM documents WHERE id=%s", (doc_id,), fetchone=True)
    if not doc:
        return jsonify({"error": "Олдсонгүй"}), 404
    folder = UPLOAD_ROOT / f"{doc['entity_type']}s"
    path = folder / doc["stored_name"]
    if not path.exists():
        return jsonify({"error": "Файл байхгүй"}), 404
    return send_file(str(path), as_attachment=True, download_name=doc["filename"])


@app.route("/api/documents/<int:doc_id>", methods=["DELETE"])
@login_required
def delete_document(doc_id):
    doc = query("SELECT * FROM documents WHERE id=%s", (doc_id,), fetchone=True)
    if doc:
        folder = UPLOAD_ROOT / f"{doc['entity_type']}s"
        path = folder / doc["stored_name"]
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass
        query("DELETE FROM documents WHERE id=%s", (doc_id,), commit=True)
    return jsonify({"success": True})


# ============================================================
# СТАТИСТИК
# ============================================================

@app.route("/api/stats")
@login_required
def stats():
    emp = query("SELECT COUNT(*) as cnt, COUNT(DISTINCT department) as depts FROM employees", fetchone=True)
    eq = query("SELECT COUNT(*) as cnt, COALESCE(SUM(quantity),0) as total_qty FROM equipment", fetchone=True)
    ct = query("SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total_amount FROM contracts", fetchone=True)
    docs = query("SELECT COUNT(*) as cnt FROM documents", fetchone=True)
    return jsonify({
        "employees": serialize(emp),
        "equipment": serialize(eq),
        "contracts": serialize(ct),
        "documents": serialize(docs),
    })


# ============================================================
# AI — Тендерийн шаардлагыг нөөцтэй харьцуулах
# ============================================================

@app.route("/api/ai/match-resources", methods=["POST"])
@login_required
def match_resources():
    """Тендерийн шаардлага дээр суурилж ажилтан, тоног төхөөрөмж, гэрээ санал болгох"""
    try:
        import anthropic
    except ImportError:
        return jsonify({"error": "anthropic SDK суулгаагүй"}), 500

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify({"error": "ANTHROPIC_API_KEY тохируулаагүй"}), 500

    data = request.get_json()
    tender_no = data.get("tender_no", "").strip()
    requirements = data.get("requirements", "").strip()

    if not requirements and tender_no:
        row = query("SELECT requirements FROM tender_details WHERE tender_no=%s", (tender_no,), fetchone=True)
        if row:
            requirements = row.get("requirements", "")

    if not requirements:
        return jsonify({"error": "Шаардлага оруулна уу"}), 400

    # Бүх нөөцийг авах (хязгаартайгаар)
    employees = query("SELECT id, last_name, first_name, position, education, profession, experience_years FROM employees LIMIT 200", fetchall=True)
    equipment = query("SELECT id, name, category, model, quantity, condition, capacity FROM equipment LIMIT 200", fetchall=True)
    contracts = query("SELECT id, contract_no, name, customer, amount, status FROM contracts LIMIT 200", fetchall=True)

    emp_txt = "\n".join([f"#{e['id']} {e['last_name']} {e['first_name']} | {e['position']} | {e['education']} {e['profession']} | {e['experience_years']} жил" for e in employees])
    eq_txt = "\n".join([f"#{e['id']} {e['name']} ({e['category']}) | {e['model']} | {e['quantity']} ш | {e['condition']}" for e in equipment])
    ct_txt = "\n".join([f"#{c['id']} {c['name']} | {c['customer']} | {c['amount']}₮ | {c['status']}" for c in contracts])

    from decimal import Decimal
    prompt = f"""Тендерийн шаардлага дээр суурилж манай компанийн нөөцөөс тохирохыг сонго.

=== ТЕНДЕРИЙН ШААРДЛАГА ===
{requirements[:3000]}

=== АЖИЛТАН ===
{emp_txt[:2000]}

=== ТОНОГ ТӨХӨӨРӨМЖ ===
{eq_txt[:2000]}

=== ИЖИЛ ТӨСТЭЙ ГЭРЭЭ ===
{ct_txt[:2000]}

ДААЛГАВАР:
Шаардлагыг хангах боломжтой ажилтан, тоног төхөөрөмж, гэрээг санал болго.
JSON хариул:
{{
  "summary": "Ерөнхий дүгнэлт",
  "employees": [{{"id": N, "reason": "яагаад тохирох"}}],
  "equipment": [{{"id": N, "reason": "..."}}],
  "contracts": [{{"id": N, "reason": "..."}}],
  "missing": ["дутуу байгаа шаардлага 1", "..."]
}}
Зөвхөн JSON."""

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()

    result = None
    try:
        result = json.loads(raw)
    except:
        s, e = raw.find('{'), raw.rfind('}')
        if s >= 0 and e > s:
            try:
                result = json.loads(raw[s:e+1])
            except:
                pass
    if not result:
        result = {"summary": raw[:500], "employees": [], "equipment": [], "contracts": [], "missing": []}

    # ID-аар бүрэн мэдээлэл нэмэх
    def enrich(items, entity):
        result_items = []
        for item in items:
            _id = item.get("id")
            if _id:
                row = query(f"SELECT * FROM {entity} WHERE id=%s", (_id,), fetchone=True)
                if row:
                    item["detail"] = serialize(row)
                    result_items.append(item)
        return result_items

    result["employees"] = enrich(result.get("employees", []), "employees")
    result["equipment"] = enrich(result.get("equipment", []), "equipment")
    result["contracts"] = enrich(result.get("contracts", []), "contracts")

    return jsonify(result)


# ============================================================
# HTML PAGES
# ============================================================

@app.route("/")
def index():
    return send_file(str(BASE_DIR / "index.html"))


@app.route("/<page>")
def page(page):
    if page in ("employees", "equipment", "contracts", "match"):
        return send_file(str(BASE_DIR / f"{page}.html"))
    return "Not found", 404


if __name__ == "__main__":
    init_db()
    # Inbox watcher background thread
    t = threading.Thread(target=_inbox_watcher, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5001))
    print(f"[resources] Эхэллээ: http://localhost:{port}")
    print(f"[resources] Inbox хавтас: {INBOX_ROOT}")
    app.run(host="0.0.0.0", port=port, debug=True)
