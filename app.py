from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
from zoneinfo import ZoneInfo
import os
from functools import wraps
from datetime import datetime, timedelta, date
import re
import sqlite3

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production-32chars")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
APP_NAME = os.environ.get("APP_NAME", "NEKEZA").strip()
APP_VERSION = os.environ.get("APP_VERSION", "2026.10.16.2").strip()
VAPID_PUBLIC_KEY = os.environ.get(
    "VAPID_PUBLIC_KEY",
    "BF7MORIX2kXdhD10vwKfBOIL_wyng5MaMRYc2F0heGaa0w_L1p-4hkvwiYr7poOXmrHG-m_-NiJH8YOipIoQKaw",
).strip()
VAPID_PRIVATE_KEY = os.environ.get(
    "VAPID_PRIVATE_KEY",
    "QDcBDM3i9vH70nDNHdY0V-TA8nK1sFShGKT1Lg0wR2s",
).strip()
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@nekeza.app").strip()
# Firebase Cloud Messaging (push haraka hata app imefungwa)
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "").strip()
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "").strip()
FIREBASE_MESSAGING_SENDER_ID = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "").strip()
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "").strip()
FIREBASE_VAPID_KEY = os.environ.get("FIREBASE_VAPID_KEY", "").strip()  # Web Push cert key from Firebase Console
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()  # service account JSON string
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "").strip()  # e.g. https://nekeza.vercel.app (for APK/TWA)


# Database: Supabase/Postgres via DATABASE_URL, else local SQLite
_raw_db = os.environ.get("DATABASE_URL", "").strip()
DATABASE_URL = _raw_db.replace("postgres://", "postgresql://", 1) if _raw_db else ""
USE_PG = DATABASE_URL.startswith("postgresql")
SQLITE_PATH = os.environ.get("SQLITE_PATH", "database.db")

# IntegrityError works on both engines
try:
    import psycopg2
    IntegrityErrors = (sqlite3.IntegrityError, psycopg2.IntegrityError)
except ImportError:
    IntegrityErrors = (sqlite3.IntegrityError,)


class CompatRow:
    """Row that supports both row['col'] and row[0] like sqlite3.Row."""
    __slots__ = ("_d", "_v")

    def __init__(self, mapping):
        if hasattr(mapping, "keys"):
            self._d = dict(mapping)
            self._v = list(mapping.values()) if not isinstance(mapping, dict) else list(mapping.values())
        else:
            self._d = dict(mapping)
            self._v = list(self._d.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._v[key]
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)

    def keys(self):
        return self._d.keys()

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def __repr__(self):
        return f"CompatRow({self._d!r})"


class CursorWrapper:
    def __init__(self, cur, use_pg):
        self._cur = cur
        self._use_pg = use_pg

    def execute(self, sql, params=None):
        sql2 = _adapt_sql(sql, self._use_pg)
        if params is None:
            self._cur.execute(sql2)
        else:
            self._cur.execute(sql2, tuple(params))
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._use_pg:
            if isinstance(row, dict):
                return CompatRow(row)
            cols = [d[0] for d in self._cur.description]
            return CompatRow(dict(zip(cols, row)))
        return row  # sqlite3.Row already supports both

    def fetchall(self):
        rows = self._cur.fetchall()
        if not self._use_pg:
            return rows
        out = []
        for row in rows:
            if isinstance(row, dict):
                out.append(CompatRow(row))
            else:
                cols = [d[0] for d in self._cur.description]
                out.append(CompatRow(dict(zip(cols, row))))
        return out

    def close(self):
        self._cur.close()


class ConnWrapper:
    def __init__(self, conn, use_pg):
        self._conn = conn
        self._use_pg = use_pg

    def execute(self, sql, params=None):
        if self._use_pg:
            import psycopg2.extras
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self._conn.cursor()
        cw = CursorWrapper(cur, self._use_pg)
        cw.execute(sql, params)
        return cw

    def executescript(self, script):
        if self._use_pg:
            # Split on semicolons carefully for PG
            for stmt in _split_sql(script):
                stmt = stmt.strip()
                if stmt:
                    self._conn.cursor().execute(stmt)
        else:
            self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass


def _split_sql(script):
    parts = []
    buf = []
    for line in script.splitlines():
        s = line.strip()
        if s.startswith("--"):
            continue
        buf.append(line)
        if ";" in line:
            chunk = "\n".join(buf)
            for piece in chunk.split(";"):
                piece = piece.strip()
                if piece:
                    parts.append(piece)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _adapt_sql(sql, use_pg):
    if not use_pg:
        return sql
    # DATE(timestamp) -> (timestamp::timestamp)::date for Postgres TEXT timestamps
    sql = re.sub(
        r"\bDATE\s*\(\s*timestamp\s*\)",
        "(timestamp::timestamp)::date",
        sql,
        flags=re.IGNORECASE,
    )
    # Boolean 0/1 -> FALSE/TRUE (works in WHERE and SET on Postgres)
    sql = re.sub(r"\b(is_void|initial_pin_used|is_read)\s*=\s*0\b", r"\1 = FALSE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\b(is_void|initial_pin_used|is_read)\s*=\s*1\b", r"\1 = TRUE", sql, flags=re.IGNORECASE)
    # ? placeholders -> %s
    sql = sql.replace("?", "%s")
    return sql



def get_db():
    if USE_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return ConnWrapper(conn, True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return ConnWrapper(conn, False)


def init_db():
    """Create tables (SQLite or Postgres). Safe to run multiple times."""
    if USE_PG:
        ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            boss_id INTEGER,
            mother_name TEXT,
            pin_expiry DATE,
            initial_pin_used BOOLEAN DEFAULT FALSE,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            buy_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            price DOUBLE PRECISION NOT NULL,
            start_quantity INTEGER NOT NULL DEFAULT 0,
            quantity INTEGER NOT NULL DEFAULT 0,
            sold_quantity INTEGER NOT NULL DEFAULT 0,
            profit_per_unit DOUBLE PRECISION DEFAULT 0,
            boss_id INTEGER NOT NULL,
            added_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sales (
            id SERIAL PRIMARY KEY,
            product_id INTEGER,
            quantity INTEGER,
            total DOUBLE PRECISION,
            employee_id INTEGER,
            boss_id INTEGER NOT NULL,
            timestamp TEXT,
            is_void BOOLEAN DEFAULT FALSE,
            void_reason TEXT,
            status TEXT,
            reason TEXT
        );
        
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            guest_name TEXT,
            guest_phone TEXT,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            is_read BOOLEAN DEFAULT FALSE,
            thread_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            platform TEXT DEFAULT 'web',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            admin_reply TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            timestamp TEXT,
            "user" TEXT,
            action TEXT,
            details TEXT
        );
        """
        # Postgres: "user" is reserved — original SQLite used unquoted user
        # Keep column name as user for query compatibility: use quoted "user"
    else:
        ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            boss_id INTEGER,
            mother_name TEXT,
            pin_expiry DATE,
            initial_pin_used BOOLEAN DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            buy_price REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL,
            start_quantity INTEGER NOT NULL DEFAULT 0,
            quantity INTEGER NOT NULL DEFAULT 0,
            sold_quantity INTEGER NOT NULL DEFAULT 0,
            profit_per_unit REAL DEFAULT 0,
            boss_id INTEGER NOT NULL,
            added_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY,
            product_id INTEGER,
            quantity INTEGER,
            total REAL,
            employee_id INTEGER,
            boss_id INTEGER NOT NULL,
            timestamp TEXT,
            is_void BOOLEAN DEFAULT 0,
            void_reason TEXT,
            status TEXT,
            reason TEXT
        );
        
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            guest_name TEXT,
            guest_phone TEXT,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            is_read BOOLEAN DEFAULT 0,
            thread_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            platform TEXT DEFAULT 'web',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            admin_reply TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            user TEXT,
            action TEXT,
            details TEXT
        );
        """

    conn = get_db()
    try:
        if USE_PG:
            cur = conn._conn.cursor()
            for stmt in _split_sql(ddl):
                cur.execute(stmt)
            # Ensure status/reason exist if table was created earlier without them
            for col, typ in (("status", "TEXT"), ("reason", "TEXT")):
                try:
                    cur.execute(f"ALTER TABLE sales ADD COLUMN IF NOT EXISTS {col} {typ}")
                except Exception:
                    pass
            conn.commit()
        else:
            conn.executescript(ddl)
            # migrate missing columns on SQLite
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(sales)").fetchall()]
                # PRAGMA returns tuples via our wrapper - use raw
            except Exception:
                cols = []
            try:
                raw = sqlite3.connect(SQLITE_PATH)
                info = raw.execute("PRAGMA table_info(sales)").fetchall()
                colnames = {row[1] for row in info}
                if "status" not in colnames:
                    raw.execute("ALTER TABLE sales ADD COLUMN status TEXT")
                if "reason" not in colnames:
                    raw.execute("ALTER TABLE sales ADD COLUMN reason TEXT")
                raw.commit()
                raw.close()
            except Exception:
                pass
            conn.commit()
    finally:
        conn.close()


def create_default_admin():
    conn = get_db()
    try:
        if not conn.execute("SELECT * FROM users WHERE role=?", ("admin",)).fetchone():
            hashed = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123"))
            conn.execute(
                "INSERT INTO users (username, password, role, initial_pin_used) VALUES (?, ?, ?, ?)",
                (os.environ.get("ADMIN_USERNAME", "admin"), hashed, "admin", True if USE_PG else 1),
            )
            conn.commit()
    finally:
        conn.close()


def log_action(user, action, details):
    conn = get_db()
    try:
        timestamp = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).isoformat()
        if USE_PG:
            # column is "user" (reserved)
            conn._conn.cursor().execute(
                'INSERT INTO logs (timestamp, "user", action, details) VALUES (%s, %s, %s, %s)',
                (timestamp, user, action, details),
            )
            conn.commit()
        else:
            conn.execute(
                "INSERT INTO logs (timestamp, user, action, details) VALUES (?, ?, ?, ?)",
                (timestamp, user, action, details),
            )
            conn.commit()
    finally:
        conn.close()


#MUDA

@app.template_filter('tz')
def tz_format(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return value

# ====================== HELPER FUNCTION ======================
def get_visible_boss_id():
    """Inarudisha boss_id ambayo user anaweza kuona"""
    if 'role' not in session or 'user_id' not in session:
        return None

    if session['role'] == 'admin':
        return None
    elif session['role'] == 'boss':
        return session['user_id']
    elif session['role'] == 'employee':
        conn = get_db()
        user = conn.execute("SELECT boss_id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        return user['boss_id'] if user else None
    return None


# ====================== FORMAT YA PESA (TANZANIA) ======================
@app.template_filter('format_money')
def format_money(value):
    """Inabadilisha namba kuwa 1,250,000 format"""
    if value is None:
        return "0"
    try:
        return f"{float(value):,.0f}"
    except:
        return "0"



# ====================== DECORATORS ======================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Tafadhali ingia kwanza.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('role') not in allowed_roles:
                flash("Huna ruhusa ya kufikia ukurasa huu.", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ====================== ROUTES ======================
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            # Check expiry kwa Boss
            if user['role'] == 'boss' and user['pin_expiry']:
                try:
                    pe = user['pin_expiry']
                    if hasattr(pe, 'year'):  # date/datetime from Postgres
                        expiry_date = pe if isinstance(pe, date) and not isinstance(pe, datetime) else pe.date() if hasattr(pe, 'date') else pe
                    else:
                        expiry_date = datetime.strptime(str(pe)[:10], "%Y-%m-%d").date()
                    if expiry_date < date.today():
                        flash("PIN yako imekwisha muda wake. Wasiliana na Admin (+255 615 864 403)", "danger")
                        return redirect(url_for('login'))
                except:
                    pass  # Kama tarehe imeharibika, pitisha

            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']

            log_action(username, "Login", "Successful login")

            if not user['initial_pin_used']:
                return redirect(url_for('change_pin'))

            # Ukumbusho malipo: flash + push, mara 1 kwa siku ikiwa siku <= 5
            if user['role'] == 'boss':
                left = days_until_expiry(user['pin_expiry'])
                if left is not None and left <= 5:
                    today_key = date.today().isoformat()
                    last_key = session.get('pay_remind_day')
                    if last_key != today_key:
                        session['pay_remind_day'] = today_key
                        if left < 0:
                            msg = "Akaunti yako imeisha muda. Fanya malipo ili uendelee kutumia huduma."
                        elif left == 0:
                            msg = "Akaunti inaisha leo. Fanya malipo au wasiliana na admin."
                        else:
                            msg = f"Siku {left} zimebaki. Tafadhali fanya malipo ili kuongeza muda wa akaunti."
                        flash(msg, "warning")
                        try:
                            _send_web_push([user['id']], APP_NAME, msg, "/settings")
                        except Exception:
                            pass

            return redirect(url_for('dashboard'))
        else:
            flash("Username au Password si sahihi.", "danger")

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    log_action(session.get('username'), "Logout", "User logged out")
    session.clear()
    return redirect(url_for('login'))

@app.route('/change_pin', methods=['GET', 'POST'])
@login_required
def change_pin():
    if request.method == 'POST':
        new_pin = request.form['new_pin']
        confirm_pin = request.form['confirm_pin']

        if new_pin != confirm_pin:
            flash("Password mpya hazifanani.", "danger")
            return render_template('change_pin.html')

        hashed = generate_password_hash(new_pin)
        conn = get_db()
        conn.execute("UPDATE users SET password = ?, initial_pin_used = 1 WHERE id = ?",
                     (hashed, session['user_id']))
        conn.commit()
        conn.close()

        flash("Password imebadilishwa kwa mafanikio!", "success")
        return redirect(url_for('dashboard'))

    return render_template('change_pin.html')


@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    boss_id = get_visible_boss_id()

    conn = get_db()
    today = date.today().strftime("%Y-%m-%d")

    if role == 'admin':
        today_sales = conn.execute("""
    SELECT COALESCE(SUM(total),0)
    FROM sales
    WHERE DATE(timestamp) = ? AND is_void=0
""", (today,)).fetchone()[0]
        total_balance = conn.execute("SELECT COALESCE(SUM(total),0) FROM sales WHERE is_void=0").fetchone()[0]
        top_product = "Admin Mode"
    else:
        # Mauzo ya leo
        today_sales = conn.execute("""
    SELECT COALESCE(SUM(total),0)
    FROM sales
    WHERE boss_id = ? AND DATE(timestamp) = ? AND is_void=0
""", (boss_id, today)).fetchone()[0]

        # Jumla ya biashara
        total_balance = conn.execute("""
            SELECT COALESCE(SUM(total),0)
            FROM sales
            WHERE boss_id = ? AND is_void=0
        """, (boss_id,)).fetchone()[0]

        # Top Product
        top_data = conn.execute("""
            SELECT p.name, COALESCE(SUM(s.quantity), 0) as qty
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.boss_id = ? AND s.is_void = 0
            GROUP BY p.name
            ORDER BY qty DESC
            LIMIT 1
        """, (boss_id,)).fetchone()

        top_product = f"{top_data['name']} ({top_data['qty']} pcs)" if top_data and top_data['name'] else "Hakuna bado"

    conn.close()

    context = {
        'username': session['username'],
        'role': role,
        'today_sales': round(today_sales, 0),
        'total_balance': round(total_balance, 0),
        'top_product': top_product
    }

    if role == 'admin':
        return render_template('dashboard_admin.html', **context)
    elif role == 'boss':
        return render_template('dashboard_boss.html', **context)
    else:
        return render_template('dashboard_employee.html', **context)



# ====================== PRODUCTS ======================
@app.route('/products')
@login_required
def products():
    boss_id = get_visible_boss_id()
    conn = get_db()

    if boss_id is None:  # Admin
        prods = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    else:
        prods = conn.execute("SELECT * FROM products WHERE boss_id=? ORDER BY name", (boss_id,)).fetchall()

    inv = stock_inventory_value(prods)
    conn.close()
    return render_template('products.html', products=prods, inventory=inv)



@app.route('/add_product', methods=['GET', 'POST'])
@login_required
@role_required(['boss'])
def add_product():
    if request.method == 'POST':
        name = request.form['name'].strip()
        buy_price = float(request.form.get('buy_price', 0))
        sell_price = float(request.form['price'])
        quantity = int(request.form['quantity'])

        # VALIDATION MUHIMU
        if sell_price < buy_price:
            flash("❌ Bei ya kuuza haipaswi kuwa chini ya bei ya kununua!\nUnaweza kupata hasara.", "danger")
            return redirect(url_for('add_product'))

        # Hesabu faida
        profit_per_unit = sell_price - buy_price

        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO products
                (name, buy_price, price, start_quantity, quantity, sold_quantity, profit_per_unit, boss_id, added_by)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """, (name, buy_price, sell_price, quantity, quantity, profit_per_unit, session['user_id'], session['user_id']))

            conn.commit()
            flash("Bidhaa imeongezwa kwa mafanikio.", "success")
            # Taarifa kwa wafanyakazi wa boss huyu
            try:
                emps = conn.execute(
                    "SELECT id FROM users WHERE boss_id=? AND role=?",
                    (session["user_id"], "employee"),
                ).fetchall()
                ids = [e["id"] for e in emps]
                if ids:
                    _send_web_push(
                        ids,
                        "Bidhaa mpya",
                        f"{request.form.get('name', 'Bidhaa')} imeongezwa kwenye stock.",
                        "/products",
                    )
            except Exception as _e:
                print("push product:", _e)
        except IntegrityErrors:
            flash("❌ Bidhaa yenye jina hilo tayari ipo.", "danger")
        except Exception as e:
            flash("❌ Kuna tatizo katika kuongeza bidhaa.", "danger")
            print(e)
        finally:
            conn.close()

        return redirect(url_for('products'))

    return render_template('add_product.html')



# ====================== SALES ===================
@app.route('/sales', methods=['GET', 'POST'])
@login_required
def sales():
    boss_id = get_visible_boss_id()
    conn = get_db()

    # Tarehe ya leo (timezone sahihi)
    from zoneinfo import ZoneInfo
    today = datetime.now(
        ZoneInfo("Africa/Dar_es_Salaam")
    ).strftime("%Y-%m-%d")

    # ==================== POST - Kurekodi Mauzo ====================
    if request.method == 'POST':
        if session['role'] != 'employee':
            flash("Boss hawezi kurekodi mauzo. Ni mfanyakazi pekee anaweza.", "danger")
            conn.close()
            return redirect(url_for('sales'))

        try:
            product_id = int(request.form['product_id'])
            qty_sold = int(request.form['quantity'])

            prod = conn.execute("""
                SELECT id, name, price, quantity, buy_price
                FROM products
                WHERE id=?
            """, (product_id,)).fetchone()

            if not prod or prod['quantity'] < qty_sold:
                flash("Stock haitoshi au bidhaa haipo!", "danger")
            else:
                total = qty_sold * prod['price']
                timestamp = datetime.now(
                    ZoneInfo("Africa/Dar_es_Salaam")
                ).strftime("%Y-%m-%d %H:%M:%S")

                buy_price = prod['buy_price'] if prod['buy_price'] is not None else 0
                profit_per_unit = prod['price'] - buy_price

                conn.execute("""
                    INSERT INTO sales (product_id, quantity, total, employee_id, boss_id, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    product_id,
                    qty_sold,
                    total,
                    session['user_id'],
                    boss_id,
                    timestamp
                ))

                conn.execute("""
                    UPDATE products
                    SET quantity = quantity - ?,
                        sold_quantity = sold_quantity + ?,
                        profit_per_unit = ?
                    WHERE id = ?
                """, (
                    qty_sold,
                    qty_sold,
                    profit_per_unit,
                    product_id
                ))

                conn.commit()
                flash("Mauzo yamekamilika.", "success")
                # Taarifa kwa boss
                try:
                    _send_web_push(
                        [boss_id],
                        "Mauzo mapya",
                        f"{session.get('username')}: {qty_sold} x {prod['name']} = TSh {total:,.0f}",
                        "/sales",
                    )
                except Exception as _e:
                    print("push sale:", _e)

        except ValueError:
            flash("Tafadhali ingiza idadi sahihi.", "danger")

        except Exception as e:
            flash("Kuna tatizo katika kurekodi mauzo.", "danger")
            print("Sales Error:", str(e))

        conn.close()
        return redirect(url_for('sales'))

    # ==================== GET - Onyesha Mauzo ====================
    if session['role'] == 'employee':
        # Mfanyakazi anaona mauzo yake ya leo tu (FIX: DATE filter)
        sales_list = conn.execute("""
            SELECT s.*, p.name
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.employee_id = ?
              AND DATE(s.timestamp) = ?
            ORDER BY s.timestamp DESC
        """, (
            session['user_id'],
            today
        )).fetchall()

    else:
        # Boss anaona mauzo yote (full history)
        sales_list = conn.execute("""
            SELECT s.*, p.name
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.boss_id = ?
            ORDER BY s.timestamp DESC
        """, (
            boss_id,
        )).fetchall()

    # Bidhaa zinazoonekana kwenye form
    products = conn.execute("""
        SELECT *
        FROM products
        WHERE boss_id=? AND quantity > 0
    """, (boss_id,)).fetchall()

    conn.close()

    return render_template(
        'sales.html',
        products=products,
        sales=sales_list,
        today=today
    )



# ====================== MANAGE USERS ======================
@app.route('/manage_employees', methods=['GET', 'POST'])
@login_required
def manage_employees():
    if request.method == 'POST':
        username = request.form['username'].strip()
        initial_pin = request.form['initial_pin']
        role = request.form['role']

        if session['role'] == 'admin' and role != 'boss':
            flash("Admin anaweza kuongeza Boss tu!", "danger")
            return redirect(url_for('manage_employees'))

        if session['role'] == 'boss' and role != 'employee':
            flash("Boss anaweza kuongeza Mfanyakazi tu!", "danger")
            return redirect(url_for('manage_employees'))

        hashed = generate_password_hash(initial_pin)
        expiry = (date.today() + timedelta(days=180)).strftime("%Y-%m-%d") if role == 'boss' else None
        boss_id_for_employee = session['user_id'] if role == 'employee' else None

        conn = get_db()
        try:
            pin_flag = False if USE_PG else 0
            conn.execute("""
                INSERT INTO users (username, password, role, boss_id, pin_expiry, initial_pin_used, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (username, hashed, role, boss_id_for_employee, expiry, pin_flag, session['user_id']))
            conn.commit()
            flash(f"{role.capitalize()} '{username}' ameongezwa! Initial PIN: {initial_pin}", "success")
        except IntegrityErrors:
            flash("Username tayari linatumika. Chagua lingine.", "danger")
        finally:
            conn.close()

        return redirect(url_for('manage_employees'))

    # GET
    conn = get_db()
    if session['role'] == 'admin':
        users = conn.execute("SELECT * FROM users WHERE role != 'admin' ORDER BY role").fetchall()
    else:
        users = conn.execute("SELECT * FROM users WHERE boss_id = ? AND role='employee'", (session['user_id'],)).fetchall()
    users_info = []
    for u in users:
        users_info.append({
            "user": u,
            "days_left": days_until_expiry(u["pin_expiry"]) if u["role"] == "boss" else None,
            "pin_expiry": str(u["pin_expiry"] or "")[:10],
        })
    conn.close()

    return render_template('manage_employees.html', users=users, users_info=users_info)

# ====================== UTILITY ROUTES ======================
@app.route('/settings')
@login_required
def settings():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return render_template("settings.html", user=user)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Hariri username na taarifa nyingine za profile."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not user:
        conn.close()
        flash("Akaunti haipatikani.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()[:120]
        if len(new_username) < 3 or len(new_username) > 80:
            flash("Username iwe herufi 3–80.", "danger")
            conn.close()
            return render_template("profile.html", user=user)

        # unique username check
        other = conn.execute(
            "SELECT id FROM users WHERE username=? AND id!=?",
            (new_username, session["user_id"]),
        ).fetchone()
        if other:
            flash("Username hii tayari inatumika.", "danger")
            conn.close()
            return render_template("profile.html", user=user)

        try:
            conn.execute(
                "UPDATE users SET username=?, mother_name=? WHERE id=?",
                (new_username, full_name, session["user_id"]),
            )
            conn.commit()
            session["username"] = new_username
            flash("Profile imesasishwa.", "success")
        except IntegrityErrors:
            flash("Username hii tayari inatumika.", "danger")
        finally:
            conn.close()
        return redirect(url_for("profile"))

    conn.close()
    return render_template("profile.html", user=user)

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact_admin')
def contact_admin():
    """User chooses how to contact admin — not forced to WhatsApp."""
    return render_template(
        "contact_admin.html",
        admin_phone=os.environ.get("ADMIN_CONTACT_PHONE", "255699783062"),
        admin_email=os.environ.get("ADMIN_CONTACT_EMAIL", ""),
        admin_whatsapp=os.environ.get("ADMIN_CONTACT_WHATSAPP", "255699783062"),
    )

@app.route('/reset_pin/<int:user_id>')
@login_required
def reset_pin(user_id):
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    if not target:
        flash("User huyo haipo.", "danger")
        conn.close()
        return redirect(url_for('manage_employees'))

    can_reset = False
    if session['role'] == 'admin':
        can_reset = True
    elif session['role'] == 'boss' and target['role'] == 'employee' and target['boss_id'] == session['user_id']:
        can_reset = True

    if not can_reset:
        flash("Huna ruhusa ya kureset user huyu.", "danger")
        conn.close()
        return redirect(url_for('manage_employees'))

    new_pin = "123456"
    hashed = generate_password_hash(new_pin)
    expiry = (date.today() + timedelta(days=180)).strftime("%Y-%m-%d") if target['role'] == 'boss' else None

    conn.execute("UPDATE users SET password=?, initial_pin_used=0, pin_expiry=? WHERE id=?",
                (hashed, expiry, user_id))
    conn.commit()
    conn.close()

    flash(f"PIN ya {target['username']} imewekwa kuwa {new_pin}", "success")
    return redirect(url_for('manage_employees'))





@app.route('/export_pdf', methods=['GET', 'POST'])
@login_required
@role_required(['boss'])
def export_pdf():
    boss_id = session['user_id']
    conn = get_db()

    # Default ni leo
    filter_type = request.form.get('filter_type', 'today')
    start_date = request.form.get('start_date')
    end_date = request.form.get('end_date')

    # Build query kulingana na filter
    query = """
        SELECT s.timestamp, p.name, s.quantity, s.total, u.username as employee
        FROM sales s
        JOIN products p ON s.product_id = p.id
        LEFT JOIN users u ON s.employee_id = u.id
        WHERE s.boss_id = ? AND s.is_void = 0
    """
    params = [boss_id]

    today = date.today()

    if filter_type == 'today':
        query += " AND s.timestamp LIKE ?"
        params.append(today.strftime("%Y-%m-%d") + "%")
        report_title = f"Ripoti ya Leo - {today}"

    elif filter_type == 'weekly':
        start_of_week = today - timedelta(days=today.weekday())
        query += " AND s.timestamp >= ?"
        params.append(start_of_week.strftime("%Y-%m-%d"))
        report_title = f"Ripoti ya Wiki hii ({start_of_week.strftime('%d/%m/%Y')})"

    elif filter_type == 'monthly':
        query += " AND strftime('%Y-%m', s.timestamp) = ?"
        params.append(today.strftime("%Y-%m"))
        report_title = f"Ripoti ya Mwezi huu ({today.strftime('%B %Y')})"

    elif filter_type == 'yearly':
        query += " AND strftime('%Y', s.timestamp) = ?"
        params.append(today.strftime("%Y"))
        report_title = f"Ripoti ya Mwaka {today.year}"

    elif filter_type == 'custom' and start_date and end_date:
        query += " AND s.timestamp BETWEEN ? AND ?"
        params.extend([start_date, end_date + " 23:59:59"])
        report_title = f"Ripoti kutoka {start_date} hadi {end_date}"

    else:
        query += " AND s.timestamp LIKE ?"
        params.append(today.strftime("%Y-%m-%d") + "%")
        report_title = f"Ripoti ya Leo - {today}"

    query += " ORDER BY s.timestamp DESC"

    sales = conn.execute(query, params).fetchall()
    conn.close()

    # ==================== PDF GENERATION ====================
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)

    elements = []
    styles = getSampleStyleSheet()

    # Logo
    logo_path = os.path.join("static", "logo.png")
    if os.path.exists(logo_path):
        elements.append(Image(logo_path, width=70, height=70))

    elements.append(Spacer(1, 10))
    elements.append(Paragraph("<b>NEKEZA</b>", styles['Title']))
    elements.append(Paragraph(report_title, styles['Heading2']))
    elements.append(Spacer(1, 20))

    # Table
    data = [["Tarehe", "Mfanyakazi", "Bidhaa", "Idadi", "Jumla (TZS)"]]
    grand_total = 0

    for sale in sales:
        total_formatted = f"{float(sale['total']):,.0f}"
        grand_total += float(sale['total'])

        data.append([
            tz_format(sale['timestamp']),
            str(sale['employee'] or "-"),
            str(sale['name']),
            str(sale['quantity']),
            total_formatted
        ])

    grand_total_formatted = f"{grand_total:,.0f}"
    data.append(["", "", "", Paragraph("<b>JUMLA</b>", styles['Normal']),
                 Paragraph(f"<b>{grand_total_formatted} TZS</b>", styles['Normal'])])

    table = Table(data, colWidths=[85, 100, 170, 55, 110])
    table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (-1, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (-1, -1), (-1, -1), colors.lightgrey),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 50))

    elements.append(Paragraph("_______________________________", styles['Normal']))
    elements.append(Paragraph("Sahihi ya Boss", styles['Normal']))

    doc.build(elements)

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"NEKEZA_Report_{filter_type}.pdf",
        mimetype='application/pdf'
    )



# ====================== SETUP INITIAL (Sajiri Boss au Employee) ======================
@app.route('/setup_initial', methods=['GET', 'POST'])
@login_required
def setup_initial():
    if request.method == 'POST':
        username = request.form['username'].strip()
        initial_pin = request.form['initial_pin']
        role = request.form['role']

        # Permission Check
        if session['role'] == 'admin' and role != 'boss':
            flash("Admin anaweza kusajiri Boss tu!", "danger")
            return redirect(url_for('setup_initial'))

        if session['role'] == 'boss' and role != 'employee':
            flash("Boss anaweza kusajiri Mfanyakazi tu!", "danger")
            return redirect(url_for('setup_initial'))

        hashed = generate_password_hash(initial_pin)
        expiry = None
        boss_id_for_user = None

        if role == 'boss':
            expiry = (date.today() + timedelta(days=180)).strftime("%Y-%m-%d")
        else:  # employee
            boss_id_for_user = session['user_id']

        conn = get_db()
        try:
            pin_flag = False if USE_PG else 0
            conn.execute("""
                INSERT INTO users
                (username, password, role, boss_id, pin_expiry, initial_pin_used, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (username, hashed, role, boss_id_for_user, expiry, pin_flag, session['user_id']))

            conn.commit()
            flash(f"{role.capitalize()} '{username}' amesajiriwa! Initial PIN: {initial_pin}", "success")
        except IntegrityErrors:
            flash("Username hili tayari linatumika. Chagua lingine.", "danger")
        finally:
            conn.close()

        return redirect(url_for('manage_employees'))

    # GET request
    return render_template('setup_initial.html')



@app.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    current_user_id = session['user_id']
    current_role = session['role']

    conn = get_db()

    # Angalia user anayetaka kufutwa
    user_to_delete = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    if not user_to_delete:
        flash("Mtumiaji huyo haipo.", "danger")
        conn.close()
        return redirect(url_for('manage_employees'))

    # Admin anaweza kufuta Boss yeyote
    if current_role == 'admin':
        if user_to_delete['role'] == 'admin':
            flash("Huwezi kufuta admin mwingine.", "danger")
        else:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            flash(f"{user_to_delete['role'].capitalize()} amefutwa kwa mafanikio.", "success")

    # Boss anaweza kufuta mfanyakazi wake tu
    elif current_role == 'boss':
        if user_to_delete['role'] != 'employee' or user_to_delete['boss_id'] != current_user_id:
            flash("Unaweza kufuta mfanyakazi wako tu.", "danger")
        else:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            flash("Mfanyakazi amefutwa kwa mafanikio.", "success")

    else:
        flash("Huna ruhusa ya kufuta mtumiaji.", "danger")

    conn.close()
    return redirect(url_for('manage_employees'))




# ==================== REKODI KASORO (MFANYAKAZI) ====================





@app.route('/record_defect', methods=['GET', 'POST'])
@login_required
def record_defect():
    boss_id = get_visible_boss_id()
    conn = get_db()

    if request.method == 'POST':
        try:
            product_id = int(request.form['product_id'])
            qty = int(request.form['quantity'])
            reason = request.form['reason'].strip()

            prod = conn.execute(
                "SELECT id, name, quantity FROM products WHERE id=?",
                (product_id,)
            ).fetchone()

            if not prod:
                flash("Bidhaa haipo!", "danger")

            elif prod['quantity'] <= 0:
                flash("❌ Stock ya bidhaa hii imeisha kabisa!", "danger")

            elif prod['quantity'] < qty:
                flash(f"❌ Stock haitoshi! Iliyobaki ni {prod['quantity']} tu.", "danger")

            else:
                timestamp = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("""
                    INSERT INTO sales
                    (product_id, quantity, total, employee_id, boss_id, timestamp, status, reason)
                    VALUES (?, ?, 0, ?, ?, ?, 'pending', ?)
                """, (
                    product_id,
                    qty,
                    session['user_id'],
                    boss_id,
                    timestamp,
                    reason
                ))

                conn.commit()
                flash("Kasoro imerekodiwa. Inasubiri approval ya Boss.", "info")

        except ValueError:
            flash("Tafadhali ingiza idadi sahihi.", "danger")

        except Exception as e:
            flash("Kuna tatizo katika kurekodi kasoro.", "danger")
            print(e)

        finally:
            conn.close()

        return redirect(url_for('sales'))

    # GET - Onyesha form
    products = conn.execute(
        "SELECT * FROM products WHERE boss_id=? AND quantity > 0",
        (boss_id,)
    ).fetchall()

    conn.close()
    return render_template('record_defect.html', products=products)




# ==================== BOSS PENDING APPROVALS ====================
@app.route('/pending_approvals')
@login_required
@role_required(['boss'])
def pending_approvals():
    conn = get_db()
    pending = conn.execute("""
        SELECT s.id, s.timestamp, p.name, s.quantity, s.reason, u.username as employee
        FROM sales s
        JOIN products p ON s.product_id = p.id
        LEFT JOIN users u ON s.employee_id = u.id
        WHERE s.boss_id = ? AND s.status = 'pending'
        ORDER BY s.timestamp DESC
    """, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('pending_approvals.html', pending=pending)


@app.route('/approve_sale/<int:sale_id>', methods=['POST'])
@login_required
@role_required(['boss'])
def approve_sale(sale_id):
    conn = get_db()
    sale = conn.execute("SELECT * FROM sales WHERE id=? AND boss_id=?", (sale_id, session['user_id'])).fetchone()

    if sale and sale['status'] == 'pending':
        conn.execute("UPDATE products SET quantity = quantity - ? WHERE id=?", (sale['quantity'], sale['product_id']))
        conn.execute("UPDATE sales SET status = 'approved' WHERE id=?", (sale_id,))
        conn.commit()
        flash("Kasoro imeapproved na stock imeshushwa.", "success")
    else:
        flash("Haikuweza kupata rekodi.", "danger")
    conn.close()
    return redirect(url_for('pending_approvals'))


@app.route('/reject_sale/<int:sale_id>', methods=['POST'])
@login_required
@role_required(['boss'])
def reject_sale(sale_id):
    conn = get_db()
    conn.execute("DELETE FROM sales WHERE id=? AND boss_id=?", (sale_id, session['user_id']))
    conn.commit()
    flash("Kasoro imekataliwa na kufutwa.", "info")
    conn.close()
    return redirect(url_for('pending_approvals'))


# ==================== REVIEW YA MFANYAKAZI ====================
@app.route('/my_defects')
@login_required
def my_defects():
    conn = get_db()
    defects = conn.execute("""
        SELECT s.id, s.timestamp, p.name, s.quantity, s.reason, s.status
        FROM sales s
        JOIN products p ON s.product_id = p.id
        WHERE s.employee_id = ? AND s.status IN ('pending', 'approved', 'rejected')
        ORDER BY s.timestamp DESC
    """, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('my_defects.html', defects=defects)




# ====================== SITEMAP ======================
@app.route('/sitemap.xml')
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>https://ramkey1.pythonanywhere.com/</loc>
        <lastmod>2026-04-29</lastmod>
        <changefreq>daily</changefreq>
        <priority>1.0</priority>
    </url>
    <url>
        <loc>https://ramkey1.pythonanywhere.com/products</loc>
        <lastmod>2026-04-29</lastmod>
        <changefreq>weekly</changefreq>
        <priority>0.8</priority>
    </url>
    <url>
        <loc>https://ramkey1.pythonanywhere.com/sales</loc>
        <lastmod>2026-04-29</lastmod>
        <changefreq>daily</changefreq>
        <priority>0.9</priority>
    </url>
</urlset>'''
    return Response(xml, mimetype='application/xml')




@app.route('/debug_sales')
def debug_sales():
    conn = get_db()

    sales = conn.execute("""
        SELECT id, product_id, quantity, total, employee_id, boss_id, timestamp
        FROM sales
        ORDER BY id DESC
        LIMIT 5
    """).fetchall()

    users = conn.execute("""
        SELECT id, username, role, boss_id FROM users
    """).fetchall()

    conn.close()

    return {
        "sales": [dict(row) for row in sales],
        "users": [dict(row) for row in users]
    }





# ====================== HELPERS: EXPIRY, STOCK VALUE, PUSH ======================
def days_until_expiry(pin_expiry):
    """Return days left (int) or None if no expiry."""
    if not pin_expiry:
        return None
    try:
        if hasattr(pin_expiry, "year"):
            exp = pin_expiry if type(pin_expiry).__name__ == "date" else pin_expiry.date()
        else:
            exp = datetime.strptime(str(pin_expiry)[:10], "%Y-%m-%d").date()
        return (exp - date.today()).days
    except Exception:
        return None


def stock_inventory_value(products_rows):
    """Thamani ya duka = sum(buy_price * quantity) kwa stock iliyopo."""
    total = 0.0
    cost_total = 0.0
    retail_total = 0.0
    for p in products_rows or []:
        try:
            qty = float(p["quantity"] or 0)
            buy = float(p["buy_price"] or 0)
            price = float(p["price"] or 0)
            cost_total += buy * qty
            retail_total += price * qty
        except Exception:
            pass
    return {
        "cost_value": round(cost_total, 0),
        "retail_value": round(retail_total, 0),
        "potential_profit": round(retail_total - cost_total, 0),
    }


_firebase_app = None

def _init_firebase():
    """Init firebase-admin once (service account JSON from env)."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    if not FIREBASE_CREDENTIALS_JSON:
        return None
    try:
        import json as _json
        import firebase_admin
        from firebase_admin import credentials
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        info = _json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(info)
        _firebase_app = firebase_admin.initialize_app(cred)
        return _firebase_app
    except Exception as exc:
        print("firebase init:", exc)
        return None


def _send_fcm(user_ids, title, body="", link="/"):
    """Tuma Firebase Cloud Messaging — inafanya kazi hata app imefungwa."""
    if not user_ids:
        return
    if _init_firebase() is None:
        print("fcm: firebase not initialized (check FIREBASE_CREDENTIALS_JSON)")
        return
    try:
        from firebase_admin import messaging
    except ImportError:
        print("firebase_admin not installed")
        return
    conn = get_db()
    try:
        tokens = []
        for uid in user_ids:
            rows = conn.execute(
                "SELECT id, token FROM fcm_tokens WHERE user_id=?",
                (uid,),
            ).fetchall()
            tokens.extend(rows)
        if not tokens:
            print("fcm: no tokens for user_ids", user_ids)
            return

        base = (APP_PUBLIC_URL or "https://nekeza.dutoza.com").rstrip("/")
        full_link = link if (link or "").startswith("http") else (base + (link or "/"))

        dead = []
        for row in tokens:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(
                        title=title or APP_NAME,
                        body=body or "",
                    ),
                    data={
                        "url": full_link,
                        "title": title or APP_NAME,
                        "body": body or "",
                    },
                    token=row["token"],
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            title=title or APP_NAME,
                            body=body or "",
                            sound="default",
                            channel_id="default",
                            color="#1e3a8a",
                            priority="high",
                            default_sound=True,
                            default_vibrate_timings=True,
                        ),
                    ),
                    webpush=messaging.WebpushConfig(
                        fcm_options=messaging.WebpushFCMOptions(link=full_link),
                        notification=messaging.WebpushNotification(
                            title=title or APP_NAME,
                            body=body or "",
                            icon=base + "/static/icon-192.png",
                        ),
                    ),
                )
                messaging.send(msg)
                print("fcm: sent ok to token", str(row["token"])[:20], "...")
            except Exception as exc:
                err = str(exc).lower()
                print("fcm send error:", exc)
                if "not-found" in err or "unregistered" in err or "invalid" in err:
                    dead.append(row["id"])
        for did in dead:
            try:
                conn.execute("DELETE FROM fcm_tokens WHERE id=?", (did,))
            except Exception:
                pass
        if dead:
            conn.commit()
    finally:
        conn.close()


def _send_web_push(user_ids, title, body="", link="/"):
    """Web Push (VAPID) + Firebase FCM. FCM kwanza ikiwa imesanidiwa."""
    if not user_ids:
        return
    # 1) Firebase FCM (haraka, app imefungwa)
    try:
        _send_fcm(user_ids, title, body, link)
    except Exception as exc:
        print("fcm:", exc)
    # 2) Classic Web Push (VAPID) — backup
    if not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush
    except ImportError:
        return
    import json as _json
    conn = get_db()
    try:
        subs = []
        for uid in user_ids:
            rows = conn.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
                (uid,),
            ).fetchall()
            subs.extend(rows)
        if not subs:
            return
        payload = _json.dumps({"title": title, "body": body, "url": link})
        dead = []
        for row in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": row["endpoint"],
                        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
                    },
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                    ttl=86400,
                    timeout=12,
                )
            except Exception as exc:
                msg = str(exc)
                if "410" in msg or "404" in msg or "Gone" in msg:
                    dead.append(row["id"])
        for did in dead:
            try:
                conn.execute("DELETE FROM push_subscriptions WHERE id=?", (did,))
            except Exception:
                pass
        if dead:
            conn.commit()
    finally:
        conn.close()


@app.context_processor
def inject_globals():
    days_left = None
    unread_chat = 0
    if session.get("user_id"):
        try:
            conn = get_db()
            u = conn.execute("SELECT role, pin_expiry FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if u and u["role"] == "boss":
                days_left = days_until_expiry(u["pin_expiry"])
            if session.get("role") == "admin":
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM chat_messages WHERE sender='user' AND (is_read = FALSE OR is_read = 0 OR is_read IS NULL)"
                ).fetchone()
                unread_chat = int(row[0] if row else 0)
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM chat_messages WHERE user_id=? AND sender='admin' AND (is_read = FALSE OR is_read = 0 OR is_read IS NULL)",
                    (session["user_id"],),
                ).fetchone()
                unread_chat = int(row[0] if row else 0)
            conn.close()
        except Exception as exc:
            print("context:", exc)
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "days_left": days_left,
        "unread_chat": unread_chat,
        "vapid_public": VAPID_PUBLIC_KEY,
        "firebase_config": {
            "apiKey": FIREBASE_API_KEY,
            "authDomain": FIREBASE_AUTH_DOMAIN,
            "projectId": FIREBASE_PROJECT_ID,
            "messagingSenderId": FIREBASE_MESSAGING_SENDER_ID,
            "appId": FIREBASE_APP_ID,
        } if FIREBASE_API_KEY and FIREBASE_PROJECT_ID else None,
        "firebase_vapid_key": FIREBASE_VAPID_KEY,
    }


# ====================== LEGAL & ACCOUNT ======================

@app.route("/download")
@app.route("/download-app")
def download_app():
    return render_template("download.html")


@app.route("/api/download/apk")
def download_apk():
    """Pakua APK — headers sahihi (Vercel + browser)."""
    from flask import send_from_directory
    folder = os.path.join(app.root_path, "static", "downloads")
    path = os.path.join(folder, "nekeza-app.apk")
    if not os.path.isfile(path):
        flash("APK bado haijawekwa. Wasiliana na admin.", "error")
        return redirect(url_for("download_app"))
    return send_from_directory(
        folder,
        "nekeza-app.apk",
        as_attachment=True,
        download_name="NEKEZA-App.apk",
        mimetype="application/vnd.android.package-archive",
    )


@app.route("/static/downloads/<path:filename>")
def download_file(filename):
    """Serve APK/files from static/downloads."""
    from flask import send_from_directory
    folder = os.path.join(app.root_path, "static", "downloads")
    # Security: only allow files inside folder
    safe = os.path.basename(filename)
    return send_from_directory(
        folder,
        safe,
        as_attachment=True,
        download_name=safe,
        mimetype="application/vnd.android.package-archive" if safe.endswith(".apk") else None,
    )

@app.route("/privacy")
def privacy():
    return render_template("legal.html", page="privacy")


@app.route("/terms")
def terms():
    return render_template("legal.html", page="terms")


@app.route("/delete-account", methods=["GET", "POST"])
@login_required
def delete_account():
    if session.get("role") == "admin":
        flash("Admin hawezi kujifuta hapa.", "danger")
        return redirect(url_for("dashboard"))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_text", "").strip().upper()
        if not user or not check_password_hash(user["password"], password):
            flash("Password si sahihi.", "danger")
            conn.close()
            return render_template("delete_account.html", user=user)
        if confirm != "FUTA":
            flash('Andika neno FUTA kuthibitisha.', "danger")
            conn.close()
            return render_template("delete_account.html", user=user)
        uid = session["user_id"]
        role = session.get("role")
        try:
            if role == "boss":
                # delete employees under this boss, their data soft-scope
                emps = conn.execute("SELECT id FROM users WHERE boss_id=?", (uid,)).fetchall()
                for e in emps:
                    conn.execute("DELETE FROM users WHERE id=?", (e["id"],))
            conn.execute("DELETE FROM push_subscriptions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM chat_messages WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            flash(str(exc), "danger")
            conn.close()
            return render_template("delete_account.html", user=user)
        conn.close()
        session.clear()
        flash("Akaunti imefutwa.", "success")
        return redirect(url_for("login"))
    conn.close()
    return render_template("delete_account.html", user=user)


# ====================== ADMIN: EXTEND ACCOUNT ======================
@app.route("/admin/extend/<int:user_id>", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_extend_account(user_id):
    days = request.form.get("days", "30").strip()
    try:
        days_i = int(days)
        if days_i < 1 or days_i > 3650:
            raise ValueError("range")
    except ValueError:
        flash("Weka idadi sahihi ya siku (1–3650).", "danger")
        return redirect(url_for("manage_employees"))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "admin":
        flash("Mtumiaji hajapatikana.", "danger")
        conn.close()
        return redirect(url_for("manage_employees"))
    # Extend from today or from existing expiry if still future
    left = days_until_expiry(user["pin_expiry"])
    if left is not None and left > 0:
        base = date.today() + timedelta(days=left)
    else:
        base = date.today()
    new_exp = base + timedelta(days=days_i)
    conn.execute(
        "UPDATE users SET pin_expiry=? WHERE id=?",
        (new_exp.isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    try:
         _send_web_push([user_id], "Ujumbe kutoka Admin", body[:100], "/chat")
    except Exception as e:
        print("push chat error:", e)
    flash(f"Muda umeongezwa siku {days_i}. Inaisha {new_exp.isoformat()}.", "success")
    return redirect(url_for("manage_employees"))


@app.route("/admin/set-expiry/<int:user_id>", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_set_expiry(user_id):
    exp = request.form.get("expiry_date", "").strip()
    try:
        datetime.strptime(exp, "%Y-%m-%d")
    except ValueError:
        flash("Tarehe si sahihi (YYYY-MM-DD).", "danger")
        return redirect(url_for("manage_employees"))
    conn = get_db()
    conn.execute("UPDATE users SET pin_expiry=? WHERE id=?", (exp, user_id))
    conn.commit()
    conn.close()
    flash(f"Tarehe ya kuisha imewekwa: {exp}", "success")
    return redirect(url_for("manage_employees"))


# ====================== CUSTOMER CARE / LIVE CHAT ======================
@app.route("/support", methods=["GET", "POST"])
def support():
    """Guest + anyone: open ticket OR lookup existing ticket by phone."""
    if session.get("user_id") and session.get("role") != "admin":
        # logged-in non-admin can use live chat OR tickets
        pass

    tab = request.args.get("tab", "new")
    found = None
    messages = []
    ticket = None

    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "lookup":
            phone = "".join(c for c in request.form.get("phone", "") if c.isdigit())
            if len(phone) < 9:
                flash("Weka namba sahihi ya simu.", "danger")
                return redirect(url_for("support", tab="lookup"))
            conn = get_db()
            ticket = conn.execute(
                "SELECT * FROM support_tickets WHERE phone=? ORDER BY id DESC LIMIT 1",
                (phone,),
            ).fetchone()
            if not ticket:
                # also try last 9 digits match
                tickets = conn.execute(
                    "SELECT * FROM support_tickets ORDER BY id DESC LIMIT 100"
                ).fetchall()
                for tk in tickets:
                    p = "".join(c for c in str(tk["phone"] or "") if c.isdigit())
                    if p.endswith(phone[-9:]) or phone.endswith(p[-9:]):
                        ticket = tk
                        break
            if not ticket:
                conn.close()
                flash("Hakuna ticket kwa namba hiyo.", "warning")
                return redirect(url_for("support", tab="lookup"))
            # store phone in session for guest thread access
            session["support_phone"] = str(ticket["phone"])
            session["support_ticket_id"] = ticket["id"]
            conn.close()
            return redirect(url_for("support_ticket", ticket_id=ticket["id"]))

        if action == "create":
            name = request.form.get("name", "").strip()[:120]
            phone = request.form.get("phone", "").strip()[:20]
            message = request.form.get("message", "").strip()[:2000]
            if len(name) < 2 or len(phone) < 9 or len(message) < 3:
                flash("Jaza jina, namba na ujumbe.", "danger")
                return render_template("support.html", tab="new")
            phone_digits = "".join(c for c in phone if c.isdigit())
            conn = get_db()
            conn.execute(
                "INSERT INTO support_tickets (name, phone, message, status) VALUES (?, ?, ?, ?)",
                (name, phone_digits or phone, message, "open"),
            )
            conn.commit()
            # get id
            if USE_PG:
                row = conn.execute(
                    "SELECT id FROM support_tickets WHERE phone=? ORDER BY id DESC LIMIT 1",
                    (phone_digits or phone,),
                ).fetchone()
            else:
                row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
            tid = row["id"] if row else None
            thread = f"ticket:{tid}"
            uid = session.get("user_id")
            conn.execute(
                "INSERT INTO chat_messages (user_id, guest_name, guest_phone, sender, body, thread_key, is_read) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, name, phone_digits or phone, "user", message, thread, False if USE_PG else 0),
            )
            conn.commit()
            session["support_phone"] = phone_digits or phone
            session["support_ticket_id"] = tid
            try:
                admins = conn.execute("SELECT id FROM users WHERE role=?", ("admin",)).fetchall()
                _send_web_push(
                    [a["id"] for a in admins],
                    "Ticket mpya",
                    f"{name}: {message[:80]}",
                    "/admin/chats",
                )
            except Exception:
                pass
            conn.close()
            flash("Ticket imetumwa. Unaweza kuendelea kuchati hapa.", "success")
            if tid:
                return redirect(url_for("support_ticket", ticket_id=tid))
            return redirect(url_for("support", tab="lookup"))

    return render_template("support.html", tab=tab)


@app.route("/support/ticket/<int:ticket_id>", methods=["GET", "POST"])
def support_ticket(ticket_id):
    """User/guest chats on a ticket thread (lookup by phone session or logged-in)."""
    conn = get_db()
    ticket = conn.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        flash("Ticket haipatikani.", "danger")
        return redirect(url_for("support", tab="lookup"))

    # access control: admin always; phone match session; or logged-in owner-ish
    is_admin = session.get("role") == "admin"
    phone_ok = str(session.get("support_phone") or "") == str(ticket["phone"] or "")
    if not is_admin and not phone_ok and not session.get("user_id"):
        conn.close()
        flash("Tafuta ticket yako kwa namba ya simu kwanza.", "warning")
        return redirect(url_for("support", tab="lookup"))

    thread = f"ticket:{ticket_id}"

    if request.method == "POST":
        if ticket["status"] == "closed" and not is_admin:
            flash("Ticket imefungwa. Fungua mpya au wasiliana na admin.", "warning")
            conn.close()
            return redirect(url_for("support_ticket", ticket_id=ticket_id))
        body = request.form.get("body", "").strip()[:2000]
        if body:
            sender = "admin" if is_admin else "user"
            uid = session.get("user_id")
            conn.execute(
                "INSERT INTO chat_messages (user_id, guest_name, guest_phone, sender, body, thread_key, is_read) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uid if sender == "user" else None,
                    ticket["name"],
                    ticket["phone"],
                    sender,
                    body,
                    thread,
                    False if USE_PG else 0,
                ),
            )
            if is_admin and ticket["status"] == "open":
                pass  # stay open until closed
            conn.commit()
            # notify other side
            try:
                if is_admin:
                    # notify user if has user_id on thread messages
                    rows = conn.execute(
                        "SELECT DISTINCT user_id FROM chat_messages WHERE thread_key=? AND user_id IS NOT NULL",
                        (thread,),
                    ).fetchall()
                    ids = [r["user_id"] for r in rows if r["user_id"]]
                    if ids:
                        _send_web_push(ids, "Jibu la Admin", body[:100], f"/support/ticket/{ticket_id}")
                else:
                    admins = conn.execute("SELECT id FROM users WHERE role=?", ("admin",)).fetchall()
                    _send_web_push(
                        [a["id"] for a in admins],
                        "Ujumbe wa ticket",
                        body[:100],
                        f"/admin/ticket/{ticket_id}",
                    )
            except Exception:
                pass
        conn.close()
        if is_admin:
            return redirect(url_for("admin_ticket", ticket_id=ticket_id))
        return redirect(url_for("support_ticket", ticket_id=ticket_id))

    messages = conn.execute(
        "SELECT * FROM chat_messages WHERE thread_key=? ORDER BY id ASC",
        (thread,),
    ).fetchall()
    # mark read for current viewer
    try:
        other = "admin" if not is_admin else "user"
        conn.execute(
            "UPDATE chat_messages SET is_read=? WHERE thread_key=? AND sender=?",
            (True if USE_PG else 1, thread, other),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return render_template(
        "support_ticket.html",
        ticket=ticket,
        messages=messages,
        is_admin=is_admin,
    )


@app.route("/admin/ticket/<int:ticket_id>", methods=["GET", "POST"])
@login_required
@role_required(["admin"])
def admin_ticket(ticket_id):
    return support_ticket(ticket_id)


@app.route("/admin/ticket/<int:ticket_id>/status", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_ticket_status(ticket_id):
    status = request.form.get("status", "open").strip().lower()
    if status not in ("open", "closed"):
        flash("Status si sahihi.", "danger")
        return redirect(url_for("admin_chats"))
    conn = get_db()
    conn.execute("UPDATE support_tickets SET status=? WHERE id=?", (status, ticket_id))
    conn.commit()
    conn.close()
    flash(f"Ticket imewekwa: {status}.", "success")
    return redirect(url_for("admin_ticket", ticket_id=ticket_id))



@app.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    """Live chat for logged-in users with admin."""
    uid = session["user_id"]
    role = session.get("role")
    if role == "admin":
        return redirect(url_for("admin_chats"))
    conn = get_db()
    if request.method == "POST":
        body = request.form.get("body", "").strip()[:2000]
        if body:
            conn.execute(
                "INSERT INTO chat_messages (user_id, sender, body, is_read) VALUES (?, ?, ?, ?)",
                (uid, "user", body, False if USE_PG else 0),
            )
            conn.commit()
            try:
                admins = conn.execute("SELECT id FROM users WHERE role=?", ("admin",)).fetchall()
                _send_web_push([a["id"] for a in admins], "Chat mpya", body[:100], "/admin/chats")
            except Exception:
                pass
        return redirect(url_for("chat"))
    messages = conn.execute(
        "SELECT * FROM chat_messages WHERE user_id=? ORDER BY id ASC",
        (uid,),
    ).fetchall()
    # mark admin messages read
    try:
        conn.execute(
            "UPDATE chat_messages SET is_read=? WHERE user_id=? AND sender=?",
            (True if USE_PG else 1, uid, "admin"),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return render_template("chat.html", messages=messages)


@app.route("/admin/chats")
@login_required
@role_required(["admin"])
def admin_chats():
    conn = get_db()
    # users with messages
    rows = conn.execute(
        """
        SELECT DISTINCT user_id FROM chat_messages
        WHERE user_id IS NOT NULL
        ORDER BY user_id DESC
        """
    ).fetchall()
    threads = []
    for r in rows:
        u = conn.execute("SELECT id, username, role FROM users WHERE id=?", (r["user_id"],)).fetchone()
        if not u:
            continue
        last = conn.execute(
            "SELECT body, created_at, sender FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (r["user_id"],),
        ).fetchone()
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_messages WHERE user_id=? AND sender=? AND (is_read = FALSE OR is_read = 0 OR is_read IS NULL)",
            (r["user_id"], "user"),
        ).fetchone()
        threads.append({"user": u, "last": last, "unread": int(unread[0] if unread else 0)})
    # guest threads
    guests = conn.execute(
        """
        SELECT guest_phone, guest_name, MAX(id) as mid
        FROM chat_messages
        WHERE user_id IS NULL AND guest_phone IS NOT NULL
        GROUP BY guest_phone, guest_name
        ORDER BY mid DESC
        """
    ).fetchall()
    tickets = conn.execute(
        "SELECT * FROM support_tickets ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return render_template("admin_chats.html", threads=threads, guests=guests, tickets=tickets)


@app.route("/admin/chat/<int:user_id>", methods=["GET", "POST"])
@login_required
@role_required(["admin"])
def admin_chat_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    if request.method == "POST":
        body = request.form.get("body", "").strip()[:2000]
        if body:
            conn.execute(
                "INSERT INTO chat_messages (user_id, sender, body, is_read) VALUES (?, ?, ?, ?)",
                (user_id, "admin", body, False if USE_PG else 0),
            )
            conn.commit()
            try:
                _send_web_push([user_id], "Ujumbe kutoka Admin", body[:100], "/chat")
            except Exception:
                pass
        return redirect(url_for("admin_chat_user", user_id=user_id))
    messages = conn.execute(
        "SELECT * FROM chat_messages WHERE user_id=? ORDER BY id ASC",
        (user_id,),
    ).fetchall()
    try:
        conn.execute(
            "UPDATE chat_messages SET is_read=? WHERE user_id=? AND sender=?",
            (True if USE_PG else 1, user_id, "user"),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return render_template("admin_chat.html", chat_user=user, messages=messages)


@app.route("/api/chat/poll")
@login_required
def api_chat_poll():
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    uid = session["user_id"]
    role = session.get("role")
    conn = get_db()
    if role == "admin":
        target = request.args.get("user_id")
        if not target:
            conn.close()
            return {"items": []}
        items = conn.execute(
            "SELECT id, sender, body, created_at FROM chat_messages WHERE user_id=? AND id>? ORDER BY id ASC LIMIT 50",
            (int(target), after),
        ).fetchall()
    else:
        items = conn.execute(
            "SELECT id, sender, body, created_at FROM chat_messages WHERE user_id=? AND id>? ORDER BY id ASC LIMIT 50",
            (uid, after),
        ).fetchall()
    conn.close()
    return {
        "items": [
            {"id": m["id"], "sender": m["sender"], "body": m["body"], "created_at": str(m["created_at"] or "")}
            for m in items
        ]
    }


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    """Digital Asset Links kwa Android TWA — ficha URL bar juu."""
    # SHA256 ya signing cert (debug APK kutoka GitHub Actions)
    default_fp = "76:D6:23:21:97:F5:C3:43:2E:53:52:38:12:43:55:D2:E4:C1:1F:A0:88:B9:F8:F2:82:F3:D2:F2:93:29:D1:2D"
    raw = os.environ.get("TWA_SHA256_CERT_FINGERPRINTS", default_fp).strip()
    fps = [x.strip() for x in raw.split(",") if x.strip()]
    package = os.environ.get("TWA_PACKAGE_ID", "app.nekeza.twa").strip()
    data = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": package,
            "sha256_cert_fingerprints": fps,
        },
    }]
    return data, 200, {"Content-Type": "application/json", "Cache-Control": "no-cache"}



# ====================== PWA + PUSH API ======================
@app.route("/manifest.webmanifest")
def pwa_manifest():
    return {
        "name": APP_NAME,
        "short_name": APP_NAME,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1e3a8a",
        "theme_color": "#1e3a8a",
        "icons": [
            {"src": "/static/android-chrome-192x192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/android-chrome-512x512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, 200, {"Content-Type": "application/manifest+json"}


@app.route("/sw.js")
def service_worker():
    ver = APP_VERSION
    js = f"""
var CACHE='nekeza-{ver}';
self.addEventListener('install',function(e){{self.skipWaiting();}});
self.addEventListener('activate',function(e){{
  e.waitUntil(caches.keys().then(function(keys){{
    return Promise.all(keys.map(function(k){{if(k!==CACHE&&k.indexOf('nekeza-')===0)return caches.delete(k);}}));
  }}).then(function(){{return self.clients.claim();}}));
}});
self.addEventListener('fetch',function(e){{
  if(e.request.method!=='GET'){{e.respondWith(fetch(e.request));return;}}
  e.respondWith(fetch(e.request).catch(function(){{return caches.match(e.request);}}));
}});
self.addEventListener('push',function(e){{
  var data={{title:'{APP_NAME}',body:'Taarifa mpya',url:'/'}};
  try{{if(e.data){{var j=e.data.json();if(j)data=Object.assign(data,j);}}}}catch(err){{}}
  e.waitUntil(self.registration.showNotification(data.title,{{
    body:data.body||'',icon:'/static/icon-192.png',badge:'/static/icon-192.png',
    data:{{url:data.url||'/'}},vibrate:[200,100,200]
  }}));
}});
self.addEventListener('notificationclick',function(e){{
  e.notification.close();
  var url=(e.notification.data&&e.notification.data.url)||'/';
  e.waitUntil(clients.matchAll({{type:'window',includeUncontrolled:true}}).then(function(list){{
    for(var i=0;i<list.length;i++){{if('focus' in list[i]){{try{{list[i].navigate(url);}}catch(x){{}}return list[i].focus();}}}}
    if(clients.openWindow)return clients.openWindow(url);
  }}));
}});
"""
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/push/vapid-public")
def api_vapid_public():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def api_push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return {"ok": False}, 400
    conn = get_db()
    existing = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE push_subscriptions SET user_id=?, p256dh=?, auth=? WHERE endpoint=?",
            (session["user_id"], p256dh, auth, endpoint),
        )
    else:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)",
            (session["user_id"], endpoint, p256dh, auth),
        )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.route("/api/push/fcm-token", methods=["POST"])
@login_required
def api_fcm_token():
    """Hifadhi FCM device token (Firebase)."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "web").strip()[:20]
    if not token or len(token) < 20:
        return {"ok": False, "error": "token required"}, 400
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM fcm_tokens WHERE token=?", (token,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE fcm_tokens SET user_id=?, platform=? WHERE token=?",
                (session["user_id"], platform, token),
            )
        else:
            conn.execute(
                "INSERT INTO fcm_tokens (user_id, token, platform) VALUES (?, ?, ?)",
                (session["user_id"], token, platform),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.route("/firebase-messaging-sw.js")
def firebase_messaging_sw():
    """SW ya Firebase — inahitajika kwa background push."""
    js = f"""
/* Firebase Messaging SW — NEKEZA */
importScripts('https://www.gstatic.com/firebasejs/10.14.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.14.0/firebase-messaging-compat.js');
firebase.initializeApp({{
  apiKey: '{FIREBASE_API_KEY}',
  authDomain: '{FIREBASE_AUTH_DOMAIN}',
  projectId: '{FIREBASE_PROJECT_ID}',
  messagingSenderId: '{FIREBASE_MESSAGING_SENDER_ID}',
  appId: '{FIREBASE_APP_ID}'
}});
var messaging = firebase.messaging();
messaging.onBackgroundMessage(function(payload) {{
  var n = (payload && payload.notification) || {{}};
  var d = (payload && payload.data) || {{}};
  var title = n.title || d.title || '{APP_NAME}';
  var body = n.body || d.body || '';
  var url = d.url || '/';
  self.registration.showNotification(title, {{
    body: body,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: {{ url: url }},
    vibrate: [200, 100, 200]
  }});
}});
self.addEventListener('notificationclick', function(e) {{
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({{type:'window', includeUncontrolled:true}}).then(function(list) {{
    for (var i = 0; i < list.length; i++) {{
      if ('focus' in list[i]) {{ try {{ list[i].navigate(url); }} catch(x) {{}} return list[i].focus(); }}
    }}
    if (clients.openWindow) return clients.openWindow(url);
  }}));
}});
"""
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp



# ====================== RUN / STARTUP ======================
_schema_ready = False

@app.before_request
def _ensure_db_once():
    global _schema_ready
    if _schema_ready:
        return
    try:
        init_db()
        create_default_admin()
    except Exception as exc:
        print(f"db init warning: {exc}")
    _schema_ready = True


if __name__ == "__main__":
    init_db()
    create_default_admin()
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
