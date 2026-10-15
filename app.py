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

    conn.close()
    return render_template('products.html', products=prods)



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
            flash("✅ Bidhaa imeongezwa kwa mafanikio!", "success")
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
                flash("✅ Mauzo yamekamilika!", "success")

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
            conn.execute("""
                INSERT INTO users (username, password, role, boss_id, pin_expiry, initial_pin_used, created_by)
                VALUES (?, ?, ?, ?, ?, 0, ?)
            """, (username, hashed, role, boss_id_for_employee, expiry, session['user_id']))
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
    conn.close()

    return render_template('manage_employees.html', users=users)

# ====================== UTILITY ROUTES ======================
@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact_admin')
def contact_admin():
    return redirect("https://wa.me/255699783062?text=Hello%20NEKEZA%20Admin,%20nina%20tatizo%20na%20app%20yangu.")

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
            conn.execute("""
                INSERT INTO users
                (username, password, role, boss_id, pin_expiry, initial_pin_used, created_by)
                VALUES (?, ?, ?, ?, ?, 0, ?)
            """, (username, hashed, role, boss_id_for_user, expiry, session['user_id']))

            conn.commit()
            flash(f"✅ {role.capitalize()} '{username}' amesajiriwa! Initial PIN: {initial_pin}", "success")
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
                flash("✅ Kasoro imerekodiwa. Inasubiri approval ya Boss.", "info")

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
        flash("✅ Kasoro imeapproved na stock imeshushwa.", "success")
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
