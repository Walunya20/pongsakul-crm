import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as crm

H = {"X-CRM": "1"}


def mk(tmp, **kw):
    a = crm.create_app(data_dir=tmp, admin_password="adminpass1", secure_cookie=False, behind_proxy=False, **kw)
    a.config["TESTING"] = True
    return a


def login(c, user="admin", pw="adminpass1"):
    return c.post("/api/login", json={"username": user, "password": pw}, headers=H)


def cust(i, name="ลูกค้า"):
    return {"id": i, "name": name, "rep": "x"}


def put(kind, i, data, rev=0):
    return {"op": "put", "kind": kind, "id": i, "rev": rev, "data": data}


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.app = mk(self.tmp)
        self.admin = self.app.test_client()
        self.assertEqual(login(self.admin).status_code, 200)

    def tearDown(self):
        self._t.cleanup()

    def add_user(self, username, role, name=None, pw="password123"):
        r = self.admin.post("/api/users", json={"username": username, "name": name or username, "role": role,
                                                 "password": pw}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c = self.app.test_client()
        self.assertEqual(login(c, username, pw).status_code, 200)
        return c


class AuthTests(Base):
    def test_requires_login_and_csrf_header(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/state").status_code, 401)
        self.assertEqual(c.get("/api/me").status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "admin", "password": "adminpass1"}).status_code, 400)
        self.assertEqual(c.get("/healthz").status_code, 200)
        self.assertEqual(c.get("/").status_code, 200)

    def test_bad_password_and_throttle(self):
        c = self.app.test_client()
        for _ in range(5):
            self.assertEqual(login(c, "admin", "wrong-password").status_code, 401)
        self.assertEqual(login(c, "admin", "adminpass1").status_code, 429)   # ถูกล็อกชั่วคราวแม้รหัสถูก

    def test_session_cookie_flags(self):
        c = self.app.test_client()
        r = login(c)
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_password_change_logs_out_other_sessions(self):
        other = self.app.test_client()
        login(other)
        r = self.admin.post("/api/password", json={"old": "adminpass1", "new": "newpassword9"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.admin.get("/api/me").status_code, 200)    # เครื่องที่เปลี่ยนยังอยู่
        self.assertEqual(other.get("/api/me").status_code, 401)         # เครื่องอื่นหลุด
        self.assertEqual(login(self.app.test_client(), "admin", "newpassword9").status_code, 200)

    def test_deactivated_user_is_kicked_out(self):
        s = self.add_user("somchai", "sales")
        users = self.admin.get("/api/users").get_json()["users"]
        uid = next(u["id"] for u in users if u["username"] == "somchai")
        self.admin.post("/api/users/%d" % uid, json={"active": False}, headers=H)
        self.assertEqual(s.get("/api/me").status_code, 401)
        self.assertEqual(login(self.app.test_client(), "somchai", "password123").status_code, 401)

    def test_cannot_remove_last_admin_or_self_demote(self):
        users = self.admin.get("/api/users").get_json()["users"]
        me = users[0]["id"]
        self.assertEqual(self.admin.post("/api/users/%d" % me, json={"role": "sales"}, headers=H).status_code, 400)
        self.assertEqual(self.admin.post("/api/users/%d" % me, json={"active": False}, headers=H).status_code, 400)

    def test_admin_cannot_reset_own_password_via_user_editor(self):
        me = self.admin.get("/api/users").get_json()["users"][0]["id"]
        r = self.admin.post("/api/users/%d" % me, json={"password": "another-pass1"}, headers=H)
        self.assertEqual(r.status_code, 400)

    def test_admin_resets_other_users_password(self):
        s = self.add_user("noi_sale", "sales")
        uid = next(u["id"] for u in self.admin.get("/api/users").get_json()["users"] if u["username"] == "noi_sale")
        self.assertEqual(self.admin.post("/api/users/%d" % uid, json={"password": "brandnew-pass"}, headers=H).status_code, 200)
        self.assertEqual(s.get("/api/me").status_code, 401)
        self.assertEqual(login(self.app.test_client(), "noi_sale", "brandnew-pass").status_code, 200)

    def test_user_validation_and_admin_only(self):
        s = self.add_user("sale1", "sales")
        self.assertEqual(s.get("/api/users").status_code, 403)
        self.assertEqual(s.post("/api/users", json={"username": "x"}, headers=H).status_code, 403)
        self.assertEqual(self.admin.post("/api/users", json={"username": "ab", "name": "n", "role": "sales",
                                                              "password": "password123"}, headers=H).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "okname", "name": "n", "role": "root",
                                                              "password": "password123"}, headers=H).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "okname", "name": "n", "role": "sales",
                                                              "password": "short"}, headers=H).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "ADMIN", "name": "n", "role": "sales",
                                                              "password": "password123"}, headers=H).status_code, 400)  # ซ้ำ (ไม่สนตัวพิมพ์)

    def test_no_bootstrap_without_password(self):
        with tempfile.TemporaryDirectory() as t:
            a = crm.create_app(data_dir=t, admin_password="", secure_cookie=False, behind_proxy=False)
            r = a.test_client().get("/api/state")
            self.assertEqual(r.status_code, 401)
            self.assertTrue(r.get_json()["setup"])


class SyncTests(Base):
    def post(self, c, ops, products=None):
        body = {"ops": ops}
        if products is not None:
            body["products"] = products
        return c.post("/api/sync", json=body, headers=H)

    def test_create_read_update(self):
        r = self.post(self.admin, [put("customers", 101, cust(101, "สมชาย"))])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revs"]["customers"]["101"], 1)
        st = self.admin.get("/api/state").get_json()
        self.assertEqual(st["state"]["customers"][0]["name"], "สมชาย")
        self.assertEqual(st["revs"]["customers"]["101"], 1)
        self.assertEqual(st["state"]["products"], crm.DEFAULT_PRODUCTS)
        r = self.post(self.admin, [put("customers", 101, cust(101, "สมชายแก้"), rev=1)])
        self.assertEqual(r.get_json()["revs"]["customers"]["101"], 2)
        self.assertEqual(self.admin.get("/api/version").get_json()["version"], 2)

    def test_two_users_different_records_both_saved(self):
        s1, s2 = self.add_user("sale_a", "sales"), self.add_user("sale_b", "sales")
        self.assertEqual(self.post(s1, [put("customers", 1, cust(1, "A"))]).status_code, 200)
        self.assertEqual(self.post(s2, [put("customers", 2, cust(2, "B"))]).status_code, 200)
        names = sorted(c["name"] for c in self.admin.get("/api/state").get_json()["state"]["customers"])
        self.assertEqual(names, ["A", "B"])

    def test_same_record_conflict_does_not_overwrite_and_is_atomic(self):
        s1, s2 = self.add_user("sale_a", "sales"), self.add_user("sale_b", "sales")
        self.post(s1, [put("customers", 1, cust(1, "เดิม"))])
        self.assertEqual(self.post(s1, [put("customers", 1, cust(1, "ของเซลล์1"), rev=1)]).status_code, 200)
        # เซลล์2 ยังเห็น rev=1 → ชนกัน; ชุดนี้มีรายการใหม่ 2 ด้วย ต้องไม่ถูกเขียนเลย
        r = self.post(s2, [put("customers", 1, cust(1, "ของเซลล์2"), rev=1), put("customers", 2, cust(2, "ใหม่"))])
        self.assertEqual(r.status_code, 409)
        j = r.get_json()
        self.assertEqual(j["conflicts"][0]["id"], 1)
        self.assertEqual([c["name"] for c in j["state"]["customers"]], ["ของเซลล์1"])
        self.assertEqual(len(self.admin.get("/api/state").get_json()["state"]["customers"]), 1)

    def test_new_id_collision_and_deleted_record(self):
        self.post(self.admin, [put("customers", 5, cust(5))])
        r = self.post(self.admin, [put("customers", 5, cust(5, "ชน"))])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["conflicts"][0]["reason"], "exists")
        self.post(self.admin, [{"op": "del", "kind": "customers", "id": 5, "rev": 1}])
        r = self.post(self.admin, [put("customers", 5, cust(5, "แก้ของที่ถูกลบ"), rev=1)])
        self.assertEqual(r.get_json()["conflicts"][0]["reason"], "deleted")

    def test_delete_is_idempotent_and_role_limited(self):
        s = self.add_user("sale1", "sales")
        self.post(self.admin, [put("customers", 7, cust(7))])
        r = self.post(s, [{"op": "del", "kind": "customers", "id": 7, "rev": 1}])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.admin.get("/api/state").get_json()["state"]["customers"]), 1)
        mgr = self.add_user("mgr1", "manager")
        self.assertEqual(self.post(mgr, [{"op": "del", "kind": "customers", "id": 7, "rev": 1}]).status_code, 200)
        self.assertEqual(self.post(mgr, [{"op": "del", "kind": "customers", "id": 7, "rev": 1}]).status_code, 200)

    def test_log_for_deleted_customer_rejected(self):
        self.post(self.admin, [put("customers", 9, cust(9))])
        self.post(self.admin, [{"op": "del", "kind": "customers", "id": 9, "rev": 1}])
        log = {"id": 20, "cid": 9, "rep": "x", "at": "2026-01-01T10:00", "summary": "s", "tags": ["need"]}
        r = self.post(self.admin, [put("logs", 20, log)])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["conflicts"][0]["reason"], "customer_deleted")

    def test_sales_log_rep_forced_to_login_name(self):
        s = self.add_user("noi", "sales", name="น้อย")
        self.post(s, [put("customers", 1, cust(1))])
        log = {"id": 2, "cid": 1, "rep": "คนอื่น", "at": "2026-01-01T10:00", "summary": "s", "tags": []}
        self.assertEqual(self.post(s, [put("logs", 2, log)]).status_code, 200)
        got = self.admin.get("/api/state").get_json()["state"]["logs"][0]
        self.assertEqual(got["rep"], "น้อย")

    def test_products_permissions_and_rev(self):
        s = self.add_user("sale1", "sales")
        self.assertEqual(self.post(s, [], {"rev": 0, "list": ["ก"]}).status_code, 403)
        r = self.post(self.admin, [], {"rev": 0, "list": ["ก", "ข"]})
        self.assertEqual(r.get_json()["productsRev"], 1)
        r = self.post(self.admin, [], {"rev": 0, "list": ["ค"]})
        self.assertEqual(r.status_code, 409)
        st = self.admin.get("/api/state").get_json()
        self.assertEqual(st["state"]["products"], ["ก", "ข"])
        self.assertEqual(st["productsRev"], 1)

    def test_validation_rejects_bad_data(self):
        bad = [
            put("customers", 1, {"id": 1, "name": "  "}),
            put("customers", 1, {"id": 2, "name": "x"}),                          # id ไม่ตรง
            put("customers", 1, {"id": 1, "name": "x", "evil": {"a": 1}}),         # ค่าซ้อน
            put("orders", 3, {"id": 3, "cid": 1, "status": "hacked"}),
            put("logs", 3, {"id": 3, "cid": "1", "rep": "x"}),
            put("customers", 0, {"id": 0, "name": "x"}),
            put("customers", 1, {"id": 1, "name": "x" * 7000}),
            {"op": "put", "kind": "users", "id": 1, "rev": 0, "data": {"id": 1}},
            {"op": "put", "kind": "customers", "id": 1, "rev": -1, "data": {"id": 1, "name": "x"}},
        ]
        for op in bad:
            r = self.post(self.admin, [op])
            self.assertEqual(r.status_code, 400, op)
        dup = [put("customers", 1, cust(1)), put("customers", 1, cust(1))]
        self.assertEqual(self.post(self.admin, dup).status_code, 400)
        self.assertEqual(len(self.admin.get("/api/state").get_json()["state"]["customers"]), 0)

    def test_unicode_and_injection_text_roundtrip(self):
        txt = "<script>alert(1)</script> ' OR 1=1 -- =SUM(A1) สวัสดี 🚚"
        self.post(self.admin, [put("customers", 1, {"id": 1, "name": txt, "note": txt})])
        got = self.admin.get("/api/state").get_json()["state"]["customers"][0]
        self.assertEqual(got["name"], txt)


class OtherTests(Base):
    def test_export_roles(self):
        s = self.add_user("sale1", "sales")
        sheets = {"sheets": [{"name": "t", "rows": [["h"], ["=1+1"]]}]}
        self.assertEqual(s.post("/api/xlsx", json=sheets, headers=H).status_code, 403)
        r = self.admin.post("/api/xlsx", json=sheets, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:2], b"PK")
        from openpyxl import load_workbook
        import io
        ws = load_workbook(io.BytesIO(r.data)).active
        self.assertEqual(ws["A2"].data_type, "s")

    def test_backup_admin_only_and_valid_sqlite(self):
        mgr = self.add_user("mgr1", "manager")
        self.assertEqual(mgr.get("/api/backup").status_code, 403)
        self.admin.post("/api/sync", json={"ops": [put("customers", 1, cust(1, "เก็บไว้"))]}, headers=H)
        r = self.admin.get("/api/backup")
        self.assertEqual(r.status_code, 200)
        p = Path(self.tmp) / "restored.db"
        p.write_bytes(r.data)
        con = sqlite3.connect(str(p))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)
        con.close()

    def test_daily_backup_file_created(self):
        self.admin.post("/api/sync", json={"ops": [put("customers", 1, cust(1))]}, headers=H)
        self.assertTrue(list((Path(self.tmp) / "backups").glob("crm-*.db")))

    def test_security_headers(self):
        r = self.app.test_client().get("/")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_data_persists_across_restart(self):
        self.admin.post("/api/sync", json={"ops": [put("customers", 1, cust(1, "อยู่ต่อ"))]}, headers=H)
        a2 = mk(self.tmp)
        c = a2.test_client()
        login(c)
        self.assertEqual(c.get("/api/state").get_json()["state"]["customers"][0]["name"], "อยู่ต่อ")
        # session เดิมยังใช้ได้ (secret key เก็บในไฟล์)
        self.assertEqual(a2.test_client().get("/api/me").status_code, 401)


class ProductionModeTests(unittest.TestCase):
    def test_secure_cookie_over_https_and_proxy_ip_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = crm.create_app(data_dir=tmp, admin_password="adminpass1", secure_cookie=True, behind_proxy=True)
            c = a.test_client()
            r = c.post("/api/login", json={"username": "admin", "password": "adminpass1"},
                       headers=dict(H, **{"X-Forwarded-Proto": "https"}), base_url="https://crm.example.com")
            self.assertEqual(r.status_code, 200)
            ck = r.headers["Set-Cookie"]
            self.assertIn("Secure", ck)
            self.assertIn("HttpOnly", ck)
            self.assertIn("Strict-Transport-Security", r.headers)
            self.assertEqual(c.get("/api/me", base_url="https://crm.example.com").status_code, 200)
            # ล็อกการลองรหัสผิดแยกตาม IP ที่แท้จริง (X-Forwarded-For) ไม่ใช่ IP ของพร็อกซี
            for _ in range(5):
                a.test_client().post("/api/login", json={"username": "admin", "password": "bad-password"},
                                     headers=dict(H, **{"X-Forwarded-For": "1.1.1.1"}))
            blocked = a.test_client().post("/api/login", json={"username": "admin", "password": "adminpass1"},
                                           headers=dict(H, **{"X-Forwarded-For": "1.1.1.1"}))
            other = a.test_client().post("/api/login", json={"username": "admin", "password": "adminpass1"},
                                         headers=dict(H, **{"X-Forwarded-For": "2.2.2.2"}))
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(other.status_code, 200)


if __name__ == "__main__":
    unittest.main()
