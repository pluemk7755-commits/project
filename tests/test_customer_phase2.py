"""Acceptance tests for customer booking, proof of transfer, and reset flow.
Every test uses an isolated temporary SQLite database, never customer data.
"""
import os,sys,tempfile,unittest,hashlib
from pathlib import Path
from datetime import timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from webapp import core as c
from webapp.server import payee_settings

class CustomerPhase2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rngd_customer_phase2_')
        self.old=(c.DATA,c.DB_PATH,c.UPLOAD_DIR)
        c.DATA=Path(self.tmp.name);c.DB_PATH=c.DATA/'run.sqlite3';c.UPLOAD_DIR=c.DATA/'uploads'
        c.init_db()
        self.uid=c.register('ลูกค้า ทดสอบ','phase2@example.org','0812229911','123456')
        self.start=c.dstr((c.now()+timedelta(days=2)).replace(hour=18,minute=0,second=0,microsecond=0))
    def tearDown(self):
        c.DATA,c.DB_PATH,c.UPLOAD_DIR=self.old
        self.tmp.cleanup()
    def test_multicue_quote_and_booking(self):
        with c.connect() as conn:
            prices=[r['price_sat'] for r in conn.execute('SELECT price_sat FROM cues WHERE cue_id IN (1,2,3) ORDER BY cue_id')]
            q=c.quote(conn,1,self.start,1,user_id=self.uid,cue_ids=[1,2,3])
        self.assertEqual(q['cue_fee_sat'],sum(prices))
        self.assertEqual(len(q['cues']),3)
        bid=c.book(self.uid,1,self.start,1,cue_ids=[1,2,3])
        with c.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM booking_cues WHERE booking_id=?',(bid,)).fetchone()[0],3)
            self.assertEqual(conn.execute('SELECT cue_fee_sat FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],sum(prices))
    def test_cue_conflict_and_duplicates(self):
        c.book(self.uid,1,self.start,1,cue_ids=[1,2])
        with self.assertRaises(c.RuleError):c.book(self.uid,2,self.start,1,cue_ids=[1])
        with self.assertRaises(c.RuleError):c.book(self.uid,3,self.start,1,cue_ids=[3,3])
    def test_replace_pending_slip_then_old_disallowed(self):
        admin=self.admin();bid=c.book(self.uid,1,self.start,1)
        c.add_slip(self.uid,bid,'first.png',12000)
        with c.connect() as conn:old_id=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
        c.add_slip(self.uid,bid,'replacement.png',12000)
        with c.connect() as conn:
            rows=conn.execute('SELECT slip_id,status FROM payment_slips WHERE booking_id=? ORDER BY slip_id',(bid,)).fetchall()
        self.assertEqual([r['status'] for r in rows],['REPLACED','PENDING'])
        with self.assertRaises(c.RuleError):c.review_slip(admin,old_id,True)
        c.review_slip(admin,rows[-1]['slip_id'],True)
        with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'CONFIRMED')
    def test_replacement_cannot_change_approved_booking(self):
        admin=self.admin();bid=c.book(self.uid,1,self.start,1)
        c.add_slip(self.uid,bid,'first.png',12000)
        with c.connect() as conn:sid=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
        c.review_slip(admin,sid,True)
        with self.assertRaises(c.RuleError):c.add_slip(self.uid,bid,'evil.png',12000)
    def test_otp_gate_before_password(self):
        with self.assertRaises(c.RuleError):c.finish_verified_reset('browser-session','newpass','newpass')
        sent=[];c.request_reset('phase2@example.org',send=lambda email,code:sent.append(code))
        with self.assertRaises(c.RuleError):c.verify_reset_step('phase2@example.org','99999999','browser-session')
        c.verify_reset_step('phase2@example.org',sent[0],'browser-session')
        self.assertTrue(c.has_reset_grant('browser-session'))
        self.assertFalse(c.has_reset_grant('different-session'))
        c.finish_verified_reset('browser-session','newpass','newpass')
        self.assertFalse(c.has_reset_grant('browser-session'))
        self.assertEqual(c.login('phase2@example.org','newpass'),self.uid)
    def test_credit_does_not_increase_on_payment_or_upload(self):
        admin=self.admin();bid=c.book(self.uid,1,self.start,1)
        c.add_slip(self.uid,bid,'first.png',12000)
        with c.connect() as conn:
            sid=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
            self.assertEqual(c.credit_balance(conn,self.uid),0)
        c.review_slip(admin,sid,True)
        with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),0)
    def test_shop_settings_from_owner_files(self):
        from PIL import Image
        import json
        with open(c.DATA/'payee_config.json','w',encoding='utf-8') as file:json.dump({'shop':'ร้านทดสอบ','bank':'ทดสอบ','name':'ผู้รับทดสอบ','account':'0001112223'},file)
        Image.new('RGB',(20,20),'white').save(c.DATA/'payee_qr.png')
        self.assertTrue(payee_settings()['configured'])
        self.assertEqual(payee_settings()['shop'],'ร้านทดสอบ')
    def test_redeem_500_points_to_50_baht_and_refund_if_cancelled(self):
        first=c.book(self.uid,1,self.start,1)
        with c.connect(True) as conn:
            conn.execute('INSERT INTO points_ledger(user_id,booking_id,points) VALUES(?,?,?)',(self.uid,first,500))
        with c.connect() as conn:
            self.assertEqual(c.membership(conn,self.uid)['available_points'],500)
            price=c.quote(conn,2,self.start,1,user_id=self.uid,redeem_points=500)
            self.assertEqual(price['points_discount_sat'],5000)
        second=c.book(self.uid,2,self.start,1,redeem_points=500)
        with c.connect() as conn:
            self.assertEqual(c.membership(conn,self.uid)['available_points'],0)
            self.assertEqual(conn.execute('SELECT due_sat FROM bookings WHERE booking_id=?',(second,)).fetchone()[0],7000)
        c.cancel_booking(self.uid,second)
        with c.connect() as conn:
            self.assertEqual(c.membership(conn,self.uid)['available_points'],500)
        c.cancel_booking(self.uid,first)
    def test_prevent_spending_more_points_than_available(self):
        with self.assertRaises(c.RuleError):c.book(self.uid,1,self.start,1,redeem_points=500)
    def admin(self):
        with c.connect(True) as conn:
            return conn.execute("INSERT INTO staff_users(username,password_hash,role) VALUES(?,?,'ADMIN')",('admin',c.hash_password('123456'))).lastrowid

if __name__=='__main__':unittest.main()
