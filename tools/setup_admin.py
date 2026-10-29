"""First-install emergency admin creation. Never alters an existing staff account."""
from pathlib import Path
import sys,getpass
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from webapp import core as db
if __name__=='__main__':
 db.init_db()
 with db.connect() as c:
  if c.execute('SELECT 1 FROM staff_users LIMIT 1').fetchone():sys.exit('มีบัญชี ADMIN อยู่แล้ว ไม่แก้รหัสผ่านเดิม')
 username=input('ADMIN username: ').strip()
 if not 3<=len(username)<=64:sys.exit('Username ต้องยาว 3–64 ตัว')
 pw=getpass.getpass('Password (6–60 chars): ')
 if not db.password_ok(pw):sys.exit('รหัสผ่านต้องยาว 6–60 ตัว')
 if pw!=getpass.getpass('Confirm: '):sys.exit('รหัสผ่านไม่ตรงกัน')
 with db.connect(True) as c:c.execute("INSERT INTO staff_users(username,password_hash,role) VALUES(?,?,'ADMIN')",(username,db.hash_password(pw)))
 print('สร้าง ADMIN สำเร็จ')
