"""เครื่องมือสำหรับผู้ดูแล (ใช้ผ่านหน้า Shell ของ Render หรือบนเครื่องที่รันโปรแกรม)

  python manage.py list-users
  python manage.py reset-password ชื่อผู้ใช้        ตั้งรหัสผ่านใหม่ (และเปิดใช้งานบัญชีกลับมา) ใช้ตอนลืมรหัสผู้ดูแล
  python manage.py create-admin ชื่อผู้ใช้ "ชื่อที่แสดง"

ถ้า Shell พิมพ์รหัสแบบซ่อนไม่ได้ ให้ตั้งตัวแปร NEW_PASSWORD=... นำหน้าคำสั่งแทน
"""
import getpass
import os
import sys

import app as crm


def ask_password():
    pw = os.environ.get("NEW_PASSWORD")
    if pw:
        return pw
    a = getpass.getpass("รหัสผ่านใหม่ (อย่างน้อย %d ตัวอักษร): " % crm.MIN_PW)
    b = getpass.getpass("พิมพ์อีกครั้ง: ")
    if a != b:
        sys.exit("รหัสผ่านสองครั้งไม่ตรงกัน")
    return a


def main(argv):
    store = crm.Store(os.environ.get("CRM_DATA_DIR") or crm.BASE_DIR / "data")
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "list-users":
            for u in store.list_users():
                print("%-20s %-10s %-8s %s" % (u["username"], u["role"], "ใช้งาน" if u["active"] else "ปิด", u["name"]))
        elif cmd == "reset-password" and len(argv) == 3:
            u = store.get_user(username=argv[2].strip().lower())
            if not u:
                sys.exit("ไม่พบผู้ใช้ " + argv[2])
            store.update_user(u["id"], "cli", active=True, password=ask_password())
            print("ตั้งรหัสผ่านใหม่ให้ %s แล้ว (เครื่องที่ล็อกอินค้างอยู่จะถูกออกจากระบบ)" % u["username"])
        elif cmd == "create-admin" and len(argv) == 4:
            store.create_user(argv[2], argv[3], "admin", ask_password(), actor="cli")
            print("สร้างผู้ดูแล %s แล้ว" % argv[2])
        else:
            print(__doc__)
            return 1
    except crm.BadRequest as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
