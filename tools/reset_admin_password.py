"""Local-only reset of an existing Roo Nai Gor Dai web ADMIN password.

Run in your web project root: python reset_admin_password.py
Requires local filesystem access to the database. Does not create ADMIN users.
"""
from __future__ import annotations
import getpass
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from webapp import core


def main():
    path = core.DB_PATH
    if not path.is_file():
        sys.exit(f"ไม่พบฐานข้อมูล: {path}\nตรวจสอบตำแหน่งโปรเจกต์ก่อน")
    with core.connect() as conn:
        admins = conn.execute("SELECT staff_id, username, is_active FROM staff_users WHERE role='ADMIN' ORDER BY staff_id").fetchall()
    if not admins:
        sys.exit("ไม่พบบัญชี ADMIN เดิม (ไม่ได้สร้างบัญชีใหม่)")
    print("ADMIN ที่มีอยู่:")
    for row in admins:
        print(f"  {row['staff_id']}: {row['username']} ({'เปิดใช้งาน' if row['is_active'] else 'ปิดใช้งาน'})")
    chosen = input("พิมพ์เลข ID ของ ADMIN ที่ต้องการตั้งรหัสใหม่: ").strip()
    if not chosen.isdigit() or int(chosen) not in {r['staff_id'] for r in admins}:
        sys.exit("เลข ID ไม่ถูกต้อง ไม่ได้เปลี่ยนแปลงข้อมูล")
    pwd = getpass.getpass("รหัสผ่านใหม่ (6–60 ตัวอักษร): ")
    if not core.password_ok(pwd):
        sys.exit("รหัสผ่านต้องยาว 6–60 ตัวอักษร")
    if pwd != getpass.getpass("ยืนยันรหัสผ่านใหม่: "):
        sys.exit("รหัสผ่านไม่ตรงกัน ไม่ได้เปลี่ยนแปลงข้อมูล")
    backup_dir = path.parent / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before_admin_password_reset_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    # SQLite backup API includes committed WAL content even if source uses WAL.
    with sqlite3.connect(str(path)) as source, sqlite3.connect(str(backup_path)) as dest:
        source.backup(dest)
    new_hash = core.hash_password(pwd)
    with core.connect(True) as conn:
        conn.execute("UPDATE staff_users SET password_hash=? WHERE staff_id=? AND role='ADMIN'", (new_hash, int(chosen)))
        conn.execute("DELETE FROM auth_sessions WHERE staff_id=?", (int(chosen),))
        conn.execute("INSERT INTO audit_logs(staff_id,event,entity_type,entity_id,note) VALUES(?,?,?,?,?)", (int(chosen), 'ADMIN_PASSWORD_RESET_LOCAL', 'staff', int(chosen), 'Local administrator password recovery'))
    print("ตั้งรหัสผ่าน ADMIN ใหม่เรียบร้อย")
    print(f"สำรองฐานข้อมูล: {backup_path}")
    print("เข้าเว็บ: http://127.0.0.1:8000/staff/login")


if __name__ == '__main__':
    main()
