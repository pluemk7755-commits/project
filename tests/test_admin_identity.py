"""Regression: customer and staff identities may share usernames and passwords."""
import tempfile, unittest
from pathlib import Path
from webapp import core as db

class IdentitySeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.prior=db.DATA,db.DB_PATH,db.UPLOAD_DIR
        db.DATA=Path(self.tmp.name);db.DB_PATH=db.DATA/'test.sqlite3';db.UPLOAD_DIR=db.DATA/'uploads'
        db.init_db()

    def tearDown(self):
        db.DATA,db.DB_PATH,db.UPLOAD_DIR=self.prior
        self.tmp.cleanup()

    def test_same_username_and_password_are_independent(self):
        uid=db.register('สมาชิก ทดสอบ','same-id@example.com','0812345678','samepass')
        with db.connect(True) as c:
            c.execute('UPDATE users SET username=? WHERE user_id=?',('sharedname',uid))
            sid=c.execute('INSERT INTO staff_users(username,password_hash,role) VALUES (?,?,?)',('sharedname',db.hash_password('samepass'),'ADMIN')).lastrowid
        self.assertEqual(db.login('sharedname','samepass'),uid)
        self.assertEqual(db.login_staff('sharedname','samepass'),sid)
        customer_token,_=db.create_session(user_id=uid)
        admin_token,_=db.create_session(staff_id=sid)
        self.assertIsNone(db.get_session(customer_token)['staff_id'])
        self.assertIsNone(db.get_session(admin_token)['user_id'])

    def test_wrong_password_cannot_cross_roles(self):
        uid=db.register('สมาชิก ทดสอบ','same-id@example.com','0812345678','customerpass')
        with db.connect(True) as c:
            c.execute('UPDATE users SET username=? WHERE user_id=?',('sharedname',uid))
            c.execute('INSERT INTO staff_users(username,password_hash,role) VALUES (?,?,?)',('sharedname',db.hash_password('adminpass'),'ADMIN'))
        with self.assertRaises(db.RuleError):db.login_staff('sharedname','customerpass')
        with self.assertRaises(db.RuleError):db.login('sharedname','adminpass')
