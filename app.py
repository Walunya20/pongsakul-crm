"""CRM พงษ์สกุล — เวอร์ชันออนไลน์ (Flask + SQLite)

ออกแบบให้รันบนเซิร์ฟเวอร์ที่เปิดตลอดเวลา (เช่น Render) แล้วเปิดใช้จากเบราว์เซอร์ผ่านลิงก์ https
- ล็อกอินแยกผู้ใช้ มี 3 บทบาท: admin (ผู้ดูแล) / manager (หัวหน้า) / sales (เซลล์)
- บันทึกข้อมูลทีละรายการ (record) ไม่ส่งทั้งฐานข้อมูลทุกครั้ง คนละคนแก้คนละรายการจึงไม่ทับกัน
  ถ้าสองคนแก้รายการเดียวกันพร้อมกัน ระบบจะไม่เขียนทับเงียบ ๆ แต่แจ้งให้ผู้บันทึกทีหลังทราบ
- สำรองฐานข้อมูลรายวัน และผู้ดูแลดาวน์โหลดไฟล์สำรองได้เอง
"""
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, session
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=7))          # เวลาประเทศไทย (ไม่มี DST)
STATUSES = ("reserved", "deposit", "done", "cancelled")
KINDS = ("customers", "logs", "orders")
ROLES = ("admin", "manager", "sales")
DEFAULT_PRODUCTS = ["คอนกรีตผสมเสร็จ", "ปูนซีเมนต์", "เหล็กเส้น", "ทราย", "หิน", "อิฐมวลเบา", "อิฐบล็อก", "ท่อ PVC"]
MAX_BODY = 4 * 1024 * 1024
MAX_RECORD = 24 * 1024                    # ขนาดสูงสุดต่อ 1 รายการ (ไบต์ของ JSON)
MAX_OPS = 500
KEEP_BACKUPS = 14
MIN_PW = 8
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 600                        # วินาที

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','manager','sales')),
  pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL,
  pw_stamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  kind TEXT NOT NULL CHECK (kind IN ('customers','logs','orders')),
  id INTEGER NOT NULL,
  rev INTEGER NOT NULL,
  data TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  PRIMARY KEY (kind, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL, action TEXT NOT NULL, detail TEXT
);
"""


class BadRequest(ValueError):
    pass


class Forbidden(PermissionError):
    pass


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _s(v, n=2000):
    return "" if v is None else str(v)[:n]


# ---------------------------------------------------------------- ตรวจข้อมูลรายการ
SCALARS = (str, int, float, bool, type(None))


def check_record(kind, rid, data):
    """ตรวจว่ารายการที่เบราว์เซอร์ส่งมามีรูปแบบปลอดภัย: เป็น dict ค่าธรรมดา (หรือ list ของข้อความ) เท่านั้น"""
    if not isinstance(data, dict):
        raise BadRequest("%s %s: data must be an object" % (kind, rid))
    if data.get("id") != rid:
        raise BadRequest("%s %s: id mismatch" % (kind, rid))
    for k, v in data.items():
        if not isinstance(k, str) or len(k) > 40:
            raise BadRequest("%s %s: bad field name" % (kind, rid))
        if isinstance(v, list):
            if len(v) > 30 or not all(isinstance(x, str) and len(x) <= 80 for x in v):
                raise BadRequest("%s %s: field %s must be a short list of strings" % (kind, rid, k))
        elif not isinstance(v, SCALARS):
            raise BadRequest("%s %s: field %s has unsupported type" % (kind, rid, k))
        elif isinstance(v, str) and len(v) > 6000:
            raise BadRequest("%s %s: field %s too long" % (kind, rid, k))
    if kind == "customers":
        if not _s(data.get("name")).strip():
            raise BadRequest("customers: name is required")
    elif kind == "orders":
        if data.get("status") not in STATUSES:
            raise BadRequest("orders: invalid status")
    if kind in ("logs", "orders"):
        cid = data.get("cid")
        if not isinstance(cid, int) or isinstance(cid, bool):
            raise BadRequest("%s: cid (int) is required" % kind)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_RECORD:
        raise BadRequest("%s %s: record too large" % (kind, rid))
    return raw


def valid_id(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2 ** 53


def check_products(lst):
    if not isinstance(lst, list) or len(lst) > 300 or not all(isinstance(x, str) and 0 < len(x.strip()) <= 80 for x in lst):
        raise BadRequest("products must be a list of non-empty strings (<=80 chars)")
    return lst


# ---------------------------------------------------------------- ฐานข้อมูล
class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "crm.db"
        self._lock = threading.Lock()
        self._backup_day = None
        con = self.connect()
        try:
            con.executescript(SCHEMA)
        finally:
            con.close()

    def connect(self):
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    # ---- meta
    @staticmethod
    def _meta(con, key, default=None):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _set_meta(con, key, value):
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))

    def version(self):
        con = self.connect()
        try:
            return int(self._meta(con, "version", "0"))
        finally:
            con.close()

    def _snapshot(self, con):
        state = {k: [] for k in KINDS}
        revs = {k: {} for k in KINDS}
        for row in con.execute("SELECT kind, id, rev, data FROM records ORDER BY id"):
            state[row["kind"]].append(json.loads(row["data"]))
            revs[row["kind"]][str(row["id"])] = row["rev"]
        prod = self._meta(con, "products")
        state["products"] = json.loads(prod) if prod else list(DEFAULT_PRODUCTS)
        return {"version": int(self._meta(con, "version", "0")), "state": state, "revs": revs,
                "productsRev": int(self._meta(con, "products_rev", "0"))}

    def snapshot(self):
        con = self.connect()
        try:
            return self._snapshot(con)
        finally:
            con.close()

    # ---- บันทึกรายการ
    def sync(self, user, ops, products):
        """ใช้การเปลี่ยนแปลงทั้งชุดแบบ all-or-nothing

        ops      = [{op:'put'|'del', kind, id, rev, data?}]   rev = เลขแก้ไขที่ผู้ส่งเห็นล่าสุด (0 = สร้างใหม่)
        products = None หรือ {rev, list}
        คืนค่า (ok, payload) — ถ้า ok เป็น False แปลว่ามีรายการที่คนอื่นแก้ไปก่อน (ไม่มีอะไรถูกเขียน)
        """
        if not isinstance(ops, list) or len(ops) > MAX_OPS:
            raise BadRequest("ops must be a list of at most %d items" % MAX_OPS)
        seen = set()
        parsed = []
        can_manage = user["role"] in ("admin", "manager")
        for o in ops:
            if not isinstance(o, dict):
                raise BadRequest("op must be an object")
            kind, rid, rev, op = o.get("kind"), o.get("id"), o.get("rev"), o.get("op")
            if kind not in KINDS or op not in ("put", "del") or not valid_id(rid) or not isinstance(rev, int) \
                    or isinstance(rev, bool) or rev < 0:
                raise BadRequest("invalid op")
            if (kind, rid) in seen:
                raise BadRequest("duplicate op for %s %s" % (kind, rid))
            seen.add((kind, rid))
            if op == "del":
                if not can_manage:
                    raise Forbidden("เฉพาะหัวหน้าหรือผู้ดูแลเท่านั้นที่ลบข้อมูลได้")
                parsed.append((op, kind, rid, rev, None))
            else:
                data = o.get("data")
                if kind == "logs" and user["role"] == "sales" and isinstance(data, dict) and rev == 0:
                    data = dict(data, rep=user["name"])    # เซลล์ใหม่ล็อกชื่อผู้บันทึกตามบัญชีที่ล็อกอิน
                parsed.append((op, kind, rid, rev, check_record(kind, rid, data)))
        if products is not None:
            if not can_manage:
                raise Forbidden("เฉพาะหัวหน้าหรือผู้ดูแลเท่านั้นที่แก้รายการสินค้าได้")
            if not isinstance(products, dict) or not isinstance(products.get("rev"), int):
                raise BadRequest("products needs rev and list")
            plist = check_products(products.get("list"))
        with self._lock:
            con = self.connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                conflicts = []
                for op, kind, rid, rev, raw in parsed:
                    row = con.execute("SELECT rev FROM records WHERE kind=? AND id=?", (kind, rid)).fetchone()
                    if op == "put":
                        if rev == 0 and row:
                            conflicts.append({"kind": kind, "id": rid, "reason": "exists"})
                        elif rev > 0 and (not row or row["rev"] != rev):
                            conflicts.append({"kind": kind, "id": rid, "reason": "changed" if row else "deleted"})
                    elif row and row["rev"] != rev:
                        conflicts.append({"kind": kind, "id": rid, "reason": "changed"})
                if products is not None and int(self._meta(con, "products_rev", "0")) != products["rev"]:
                    conflicts.append({"kind": "products", "id": 0, "reason": "changed"})
                if conflicts:
                    snap = self._snapshot(con)
                    con.execute("ROLLBACK")
                    snap["conflicts"] = conflicts
                    return False, snap
                stamp = now_iso()
                new_revs = {k: {} for k in KINDS}
                deleted = []
                for op, kind, rid, rev, raw in parsed:
                    if op == "put":
                        con.execute(
                            "INSERT INTO records(kind,id,rev,data,updated_at,updated_by) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(kind,id) DO UPDATE SET rev=excluded.rev, data=excluded.data, "
                            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                            (kind, rid, rev + 1, raw, stamp, user["username"]))
                        new_revs[kind][str(rid)] = rev + 1
                    else:
                        con.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, rid))
                        deleted.append([kind, rid])
                        con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                                    (stamp, user["username"], "delete", "%s:%s" % (kind, rid)))
                # รายการลูกค้าที่ถูกลบไปแล้ว ห้ามมีบันทึก/ออเดอร์ใหม่ชี้ไปหา
                for op, kind, rid, rev, raw in parsed:
                    if op == "put" and kind in ("logs", "orders"):
                        cid = json.loads(raw)["cid"]
                        if not con.execute("SELECT 1 FROM records WHERE kind='customers' AND id=?", (cid,)).fetchone():
                            snap = self._snapshot(con)
                            con.execute("ROLLBACK")
                            snap["conflicts"] = [{"kind": kind, "id": rid, "reason": "customer_deleted"}]
                            return False, snap
                payload = {"revs": new_revs, "deleted": deleted}
                if products is not None:
                    prev = int(self._meta(con, "products_rev", "0")) + 1
                    self._set_meta(con, "products", json.dumps(plist, ensure_ascii=False))
                    self._set_meta(con, "products_rev", prev)
                    payload["productsRev"] = prev
                ver = int(self._meta(con, "version", "0")) + 1
                self._set_meta(con, "version", ver)
                con.execute("COMMIT")
                payload["version"] = ver
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                con.close()
        self.backup_daily()
        return True, payload

    # ---- ผู้ใช้
    def user_count(self):
        con = self.connect()
        try:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            con.close()

    def get_user(self, uid=None, username=None):
        con = self.connect()
        try:
            if uid is not None:
                return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        finally:
            con.close()

    def list_users(self):
        con = self.connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT id, username, name, role, active, created FROM users ORDER BY id")]
        finally:
            con.close()

    def create_user(self, username, name, role, password, actor="system"):
        username = _s(username).strip().lower()
        name = _s(name).strip()
        if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
            raise BadRequest("ชื่อผู้ใช้ใช้ได้เฉพาะ a-z 0-9 . _ - ยาว 3-30 ตัว")
        if not 1 <= len(name) <= 60:
            raise BadRequest("กรอกชื่อที่แสดง (ไม่เกิน 60 ตัวอักษร)")
        if role not in ROLES:
            raise BadRequest("บทบาทไม่ถูกต้อง")
        check_password(password)
        stamp = now_iso()
        con = self.connect()
        try:
            try:
                con.execute("INSERT INTO users(username,name,role,pw_hash,active,created,pw_stamp) VALUES(?,?,?,?,1,?,?)",
                            (username, name, role, generate_password_hash(password), stamp, secrets.token_hex(8)))
            except sqlite3.IntegrityError:
                raise BadRequest("มีชื่อผู้ใช้นี้อยู่แล้ว")
            con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                        (stamp, actor, "user_create", "%s (%s)" % (username, role)))
        finally:
            con.close()

    def update_user(self, uid, actor, name=None, role=None, active=None, password=None):
        with self._lock:
            con = self.connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                if not u:
                    con.execute("ROLLBACK")
                    raise BadRequest("ไม่พบผู้ใช้")
                new_role = u["role"] if role is None else role
                new_active = u["active"] if active is None else (1 if active else 0)
                if new_role not in ROLES:
                    con.execute("ROLLBACK")
                    raise BadRequest("บทบาทไม่ถูกต้อง")
                if u["role"] == "admin" and u["active"] and (new_role != "admin" or not new_active):
                    others = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                                         (uid,)).fetchone()[0]
                    if others == 0:
                        con.execute("ROLLBACK")
                        raise BadRequest("ต้องมีผู้ดูแลระบบที่ใช้งานอยู่อย่างน้อย 1 คน")
                sets, vals = ["role=?", "active=?"], [new_role, new_active]
                if name is not None:
                    name = _s(name).strip()
                    if not 1 <= len(name) <= 60:
                        con.execute("ROLLBACK")
                        raise BadRequest("กรอกชื่อที่แสดง (ไม่เกิน 60 ตัวอักษร)")
                    sets.append("name=?")
                    vals.append(name)
                if password is not None:
                    try:
                        check_password(password)
                    except BadRequest:
                        con.execute("ROLLBACK")
                        raise
                    sets += ["pw_hash=?", "pw_stamp=?"]
                    vals += [generate_password_hash(password), secrets.token_hex(8)]
                if new_active == 0 or password is not None or new_role != u["role"]:
                    pass  # session เดิมจะหมดอายุเองผ่าน pw_stamp / active ในการตรวจทุกคำขอ
                vals.append(uid)
                con.execute("UPDATE users SET " + ",".join(sets) + " WHERE id=?", vals)
                con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                            (now_iso(), actor, "user_update", "%s role=%s active=%s%s" % (
                                u["username"], new_role, new_active, " pw_reset" if password is not None else "")))
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                con.close()

    def set_password(self, uid, new_password):
        check_password(new_password)
        stamp = secrets.token_hex(8)       # ค่าสุ่ม ไม่ใช่เวลา — session เก่าของบัญชีนี้จะใช้ต่อไม่ได้ทันที
        con = self.connect()
        try:
            con.execute("UPDATE users SET pw_hash=?, pw_stamp=? WHERE id=?",
                        (generate_password_hash(new_password), stamp, uid))
            con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                        (now_iso(), "uid:%s" % uid, "password_change", ""))
        finally:
            con.close()
        return stamp

    def audit(self, user, action, detail=""):
        con = self.connect()
        try:
            con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                        (now_iso(), user, action, _s(detail, 300)))
        finally:
            con.close()

    # ---- สำรอง
    def backup_to(self, target):
        src = self.connect()
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def backup_daily(self):
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._backup_day == day:
            return
        folder = self.dir / "backups"
        target = folder / ("crm-%s.db" % day)
        try:
            folder.mkdir(exist_ok=True)
            if not target.exists():
                self.backup_to(target)
                for old in sorted(folder.glob("crm-*.db"))[:-KEEP_BACKUPS]:
                    old.unlink(missing_ok=True)
            self._backup_day = day
        except (OSError, sqlite3.Error):
            pass  # สำรองไม่สำเร็จไม่ควรทำให้การบันทึกหลักล้ม


def check_password(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PW:
        raise BadRequest("รหัสผ่านต้องยาวอย่างน้อย %d ตัวอักษร" % MIN_PW)
    if len(pw) > 128:
        raise BadRequest("รหัสผ่านยาวเกินไป")


# ---------------------------------------------------------------- Excel
def build_xlsx(sheets):
    if not isinstance(sheets, list) or not sheets or len(sheets) > 20:
        raise BadRequest("sheets must be a non-empty list")
    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="D6232A")
    sec_fill = PatternFill("solid", fgColor="FDECEC")
    used = set()
    total = 0
    for sh in sheets:
        rows = sh.get("rows") if isinstance(sh, dict) else None
        if not isinstance(rows, list) or not rows:
            raise BadRequest("each sheet needs rows")
        total += len(rows)
        if total > 100000:
            raise BadRequest("too many rows")
        name = re.sub(r"[\\/?*\[\]:]", "-", _s(sh.get("name"), 60) or "Sheet")[:31]
        base, k = name, 2
        while name.lower() in used:
            name = (base[:28] + "-%d" % k)[:31]
            k += 1
        used.add(name.lower())
        ws = wb.create_sheet(name)
        widths = {}
        for ri, row in enumerate(rows, start=1):
            if not isinstance(row, list):
                raise BadRequest("row must be a list")
            for ci, v in enumerate(row, start=1):
                if isinstance(v, bool) or v is None:
                    v = "" if v is None else str(v)
                elif not isinstance(v, (int, float)):
                    v = _s(v, 5000)
                c = ws.cell(row=ri, column=ci, value=v)
                if isinstance(v, str):
                    c.data_type = "s"  # เก็บเป็นข้อความเสมอ กันค่าที่ขึ้นต้นด้วย = ถูกตีเป็นสูตร
                else:
                    c.number_format = "#,##0"
                if len(row) > 1 or ci == 1:
                    widths[ci] = max(widths.get(ci, 8), min(48, len(str(v)) + 2))
                if ri == 1:
                    c.font = Font(bold=True, color="FFFFFF")
                    c.fill = head_fill
                    c.alignment = Alignment(vertical="center")
                elif len(row) == 1:
                    c.font = Font(bold=True)
                    c.fill = sec_fill
        for ci, w in widths.items():
            ws.column_dimensions[get_column_letter(ci)].width = w
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- แอป
def _load_secret(data_dir):
    env = os.environ.get("CRM_SECRET")
    if env:
        return env
    f = Path(data_dir) / "secret.key"
    if f.exists():
        return f.read_text().strip()
    key = secrets.token_hex(32)
    f.write_text(key)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return key


def create_app(data_dir=None, admin_password=None, secure_cookie=None, behind_proxy=None):
    data_dir = Path(data_dir or os.environ.get("CRM_DATA_DIR") or BASE_DIR / "data")
    store = Store(data_dir)
    app = Flask(__name__, static_folder=None)
    app.store = store
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_BODY,
        SECRET_KEY=_load_secret(data_dir),
        SESSION_COOKIE_NAME="crm_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(os.environ.get("CRM_COOKIE_SECURE", "1") != "0") if secure_cookie is None else secure_cookie,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    use_proxy = (os.environ.get("CRM_BEHIND_PROXY", "0") == "1") if behind_proxy is None else behind_proxy
    if use_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # สร้างผู้ดูแลคนแรกจากตัวแปรสภาพแวดล้อม (ทำเฉพาะตอนยังไม่มีผู้ใช้เลย)
    boot_pw = os.environ.get("CRM_ADMIN_PASSWORD", "") if admin_password is None else admin_password
    if store.user_count() == 0 and boot_pw:
        try:
            store.create_user(os.environ.get("CRM_ADMIN_USER", "admin"),
                              os.environ.get("CRM_ADMIN_NAME", "ผู้ดูแลระบบ"), "admin", boot_pw)
        except BadRequest as e:
            print("สร้างผู้ดูแลคนแรกไม่สำเร็จ:", e, file=sys.stderr)

    fails = {}                       # (ip, username) -> [count, first_ts]
    fails_lock = threading.Lock()

    def throttled(key):
        with fails_lock:
            rec = fails.get(key)
            if rec and time.time() - rec[1] > LOGIN_WINDOW:
                fails.pop(key, None)
                return False
            return bool(rec and rec[0] >= LOGIN_MAX_FAILS)

    def note_fail(key):
        with fails_lock:
            rec = fails.get(key)
            if not rec or time.time() - rec[1] > LOGIN_WINDOW:
                fails[key] = [1, time.time()]
            else:
                rec[0] += 1
            if len(fails) > 5000:
                fails.clear()

    dummy_hash = generate_password_hash("not-a-real-password")

    def current_user():
        uid = session.get("uid")
        if uid is None:
            return None
        u = store.get_user(uid=uid)
        if not u or not u["active"] or u["pw_stamp"] != session.get("pws"):
            session.clear()
            return None
        return u

    def pub(u):
        return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"]}

    def need(*roles):
        u = current_user()
        if not u:
            abort(401)
        if roles and u["role"] not in roles:
            abort(403)
        return u

    @app.before_request
    def csrf_guard():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-CRM") != "1":
            abort(400)

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def err(msg, code):
        return jsonify(error=msg), code

    @app.errorhandler(BadRequest)
    def _bad(e):
        return err(str(e), 400)

    @app.errorhandler(Forbidden)
    def _forbid(e):
        return err(str(e), 403)

    @app.errorhandler(400)
    def _400(_e):
        return err("คำขอไม่ถูกต้อง", 400)

    @app.errorhandler(401)
    def _401(_e):
        return jsonify(error="กรุณาเข้าสู่ระบบ", setup=store.user_count() == 0), 401

    @app.errorhandler(403)
    def _403(_e):
        return err("ไม่มีสิทธิ์ทำรายการนี้", 403)

    @app.errorhandler(413)
    def _413(_e):
        return err("ข้อมูลใหญ่เกินไป", 413)

    # ---- หน้าเว็บ
    @app.get("/")
    def index():
        return send_file(BASE_DIR / "static" / "index.html", mimetype="text/html")

    @app.get("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain"}

    # ---- เข้าสู่ระบบ
    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = _s(body.get("username"), 60).strip().lower()
        password = body.get("password") if isinstance(body.get("password"), str) else ""
        key = (request.remote_addr or "?", username)
        if throttled(key):
            return err("ลองผิดหลายครั้งเกินไป รอสักครู่ (ประมาณ 10 นาที) แล้วลองใหม่", 429)
        u = store.get_user(username=username) if username else None
        ok = check_password_hash(u["pw_hash"] if u else dummy_hash, password) and bool(u) and bool(u["active"])
        if not ok:
            note_fail(key)
            store.audit(username or "-", "login_fail", request.remote_addr or "")
            return err("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", 401)
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        session["pws"] = u["pw_stamp"]
        store.audit(u["username"], "login", request.remote_addr or "")
        return jsonify(user=pub(u))

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        return jsonify(user=pub(need()))

    @app.post("/api/password")
    def change_password():
        u = need()
        body = request.get_json(silent=True) or {}
        if not check_password_hash(u["pw_hash"], body.get("old") if isinstance(body.get("old"), str) else ""):
            return err("รหัสผ่านเดิมไม่ถูกต้อง", 400)
        stamp = store.set_password(u["id"], body.get("new"))
        session["pws"] = stamp           # คงการล็อกอินของตัวเอง ส่วน session อื่นของบัญชีนี้จะหลุด
        return jsonify(ok=True)

    # ---- ข้อมูล
    @app.get("/api/version")
    def get_version():
        need()
        return jsonify(version=store.version())

    @app.get("/api/state")
    def get_state():
        u = need()
        snap = store.snapshot()
        snap["me"] = pub(u)
        return jsonify(snap)

    @app.post("/api/sync")
    def sync():
        u = need()
        body = request.get_json(silent=False)
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")
        ok, payload = store.sync(pub(u), body.get("ops") or [], body.get("products"))
        if not ok:
            payload["me"] = pub(u)
            return jsonify(payload), 409
        return jsonify(payload)

    @app.post("/api/xlsx")
    def xlsx():
        need("admin", "manager")
        body = request.get_json(silent=False)
        buf = build_xlsx((body or {}).get("sheets"))
        return send_file(buf, as_attachment=True, download_name="crm-export.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/backup")
    def backup():
        u = need("admin")
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=str(data_dir))
        os.close(fd)
        try:
            store.backup_to(tmp)
            data = Path(tmp).read_bytes()
        finally:
            Path(tmp).unlink(missing_ok=True)
        store.audit(u["username"], "backup_download")
        return Response(data, mimetype="application/octet-stream", headers={
            "Content-Disposition": 'attachment; filename="crm-backup-%s.db"' % datetime.now(TZ).strftime("%Y-%m-%d")})

    # ---- ผู้ใช้ (เฉพาะผู้ดูแล)
    @app.get("/api/users")
    def users_list():
        need("admin")
        return jsonify(users=store.list_users())

    @app.post("/api/users")
    def users_create():
        a = need("admin")
        b = request.get_json(silent=True) or {}
        store.create_user(b.get("username"), b.get("name"), b.get("role"), b.get("password"), actor=a["username"])
        return jsonify(users=store.list_users())

    @app.post("/api/users/<int:uid>")
    def users_update(uid):
        a = need("admin")
        b = request.get_json(silent=True) or {}
        if uid == a["id"] and (b.get("active") is False or (b.get("role") not in (None, "admin"))):
            raise BadRequest("ปิดการใช้งานหรือลดสิทธิ์บัญชีของตัวเองไม่ได้")
        if uid == a["id"] and b.get("password"):
            raise BadRequest("เปลี่ยนรหัสผ่านของตัวเองที่ปุ่ม “เปลี่ยนรหัสผ่าน” ด้านบน")
        store.update_user(uid, a["username"], name=b.get("name"), role=b.get("role"),
                          active=b.get("active"), password=b.get("password"))
        return jsonify(users=store.list_users())

    return app


def main():
    port = int(os.environ.get("PORT", os.environ.get("CRM_PORT", "10000")))
    host = os.environ.get("CRM_HOST", "0.0.0.0")
    app = create_app()
    if app.store.user_count() == 0:
        print("ยังไม่มีผู้ใช้ในระบบ — ตั้งค่า CRM_ADMIN_PASSWORD (อย่างน้อย %d ตัวอักษร) แล้วเริ่มโปรแกรมใหม่" % MIN_PW)
    if os.environ.get("CRM_OPEN") == "1":
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:%d/" % port)).start()
    try:
        from waitress import serve
        print("CRM พงษ์สกุล (ออนไลน์) ฟังที่ %s:%d" % (host, port))
        serve(app, host=host, port=port, threads=8, max_request_body_size=MAX_BODY)
    except ImportError:
        app.run(host=host, port=port)


if __name__ == "__main__":
    main()
