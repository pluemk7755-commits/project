import os,sys,tempfile,unittest,sqlite3,threading
from pathlib import Path
from datetime import timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from webapp import core as c

class RebuildTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='rngd_web_')
  self.old=(c.DATA,c.DB_PATH,c.UPLOAD_DIR)
  c.DATA=Path(self.tmp.name);c.DB_PATH=c.DATA/'test.sqlite3';c.UPLOAD_DIR=c.DATA/'uploads'
  c.init_db();self.uid=c.register('ทดสอบ ลูกค้า','test@example.org','0812345678','p@ss x')
  self.start=(c.now()+timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)
 def tearDown(self):
  c.DATA,c.DB_PATH,c.UPLOAD_DIR=self.old;self.tmp.cleanup()
 def admin(self):
  with c.connect(True) as conn:
   cur=conn.execute("INSERT INTO staff_users(username,password_hash,role) VALUES(?,?,'ADMIN')",('root_admin',c.hash_password('pass123')))
   return cur.lastrowid
 def book(self,hours=1,table=1,credit=0):return c.book(self.uid,table,c.dstr(self.start),hours,credit_sat=credit)
 def test_01_seed_tables(self):
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM club_tables').fetchone()[0],9)
 def test_02_password_6_60_any_char(self):
  self.assertTrue(c.verify_password('xxxxxx',c.hash_password('xxxxxx')))
  self.assertTrue(c.verify_password('😁😁😁😁😁😁',c.hash_password('😁😁😁😁😁😁')))
  for p in ('abcde','x'*61):
   with self.assertRaises(c.RuleError):c.hash_password(p)
 def test_03_login(self):
  self.assertEqual(c.login('test@example.org','p@ss x'),self.uid)
  with self.assertRaises(c.RuleError):c.login('test@example.org','nope')
 def test_04_conflict(self):
  self.book()
  with self.assertRaises(c.RuleError):self.book()
 def test_05_parallel_conflict(self):
  result=[]
  def do():
   try:result.append(('ok',self.book()))
   except c.RuleError:result.append(('conflict',None))
  t1=threading.Thread(target=do);t2=threading.Thread(target=do)
  t1.start();t2.start();t1.join();t2.join()
  self.assertEqual([i[0] for i in result].count('ok'),1)
 def test_06_booking_pending(self):
  bid=self.book()
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'PENDING_SLIP')
 def test_07_slip_manual_confirmation(self):
  aid=self.admin();bid=self.book();c.add_slip(self.uid,bid,'sample.png',12000)
  with c.connect() as conn:
   slip=conn.execute('SELECT * FROM payment_slips WHERE booking_id=?',(bid,)).fetchone();self.assertEqual(slip['status'],'PENDING');self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'UNDER_REVIEW')
  c.review_slip(aid,slip['slip_id'],True)
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'CONFIRMED')
  with self.assertRaises(c.RuleError):c.review_slip(aid,slip['slip_id'],True)
 def test_08_reject_slip(self):
  aid=self.admin();bid=self.book();c.add_slip(self.uid,bid,'one.png',12000)
  with c.connect() as conn:sid=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
  c.review_slip(aid,sid,False,'ยอดไม่ตรง')
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'REJECTED')
 def test_09_reset_code_and_reuse(self):
  emails=[];c.request_reset('test@example.org',send=lambda a,b:emails.append((a,b)))
  code=emails[0][1]
  c.reset_password('test@example.org',code,'654321','654321')
  self.assertEqual(c.login('test@example.org','654321'),self.uid)
  with self.assertRaises(c.RuleError):c.reset_password('test@example.org',code,'111111','111111')
 def test_10_reset_invalid_attempt_counter(self):
  codes=[];c.request_reset('test@example.org',send=lambda _,x:codes.append(x))
  with self.assertRaises(c.RuleError):c.reset_password('test@example.org','00000000','abcdef','abcdef')
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT attempts FROM password_reset').fetchone()[0],1)
 def test_11_credit_only_no_show_no_inflation(self):
  aid=self.admin();bid=self.book();c.add_slip(self.uid,bid,'fake.png',12000)
  with c.connect() as conn:sid=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
  c.review_slip(aid,sid,True)
  past=c.dstr(c.now()-timedelta(minutes=30))
  with c.connect(True) as conn:
   conn.execute('UPDATE bookings SET start_at=?,end_at=? WHERE booking_id=?',(past,c.dstr(c.now()+timedelta(minutes=30)),bid))
  c.sweep();c.sweep()
  with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),12000)
  bid2=c.book(self.uid,2,c.dstr(self.start),1,credit_sat=12000)
  with c.connect() as conn:
   self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid2,)).fetchone()[0],'CONFIRMED')
   self.assertEqual(c.credit_balance(conn,self.uid),0)
  with c.connect(True) as conn:conn.execute('UPDATE bookings SET start_at=?,end_at=? WHERE booking_id=?',(past,c.dstr(c.now()+timedelta(minutes=30)),bid2))
  c.sweep()
  with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),12000)
 def test_12_unpaid_not_credited(self):
  bid=self.book()
  with c.connect(True) as conn:conn.execute("UPDATE bookings SET hold_expires_at=? WHERE booking_id=?",(c.dstr(c.now()-timedelta(minutes=1)),bid))
  c.sweep()
  with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),0)
 def test_13_cancel_credit_return(self):
  with c.connect(True) as conn:conn.execute("INSERT INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?,NULL,'NO_SHOW',12000)",(self.uid,))
  bid=self.book(credit=10000)
  with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),2000)
  c.cancel_booking(self.uid,bid)
  with c.connect() as conn:self.assertEqual(c.credit_balance(conn,self.uid),12000)
 def test_14_pii_delete_with_history(self):
  aid=self.admin();bid=self.book();c.delete_user(aid,self.uid)
  with c.connect() as conn:
   row=conn.execute('SELECT * FROM users WHERE user_id=?',(self.uid,)).fetchone()
   self.assertEqual(row['is_active'],0);self.assertTrue(row['email'].endswith('@invalid.local'))
   self.assertIsNotNone(conn.execute('SELECT 1 FROM bookings WHERE booking_id=?',(bid,)).fetchone())
 def test_15_staff_import_hash_compatible(self):
  aid=self.admin();self.assertEqual(c.login_staff('root_admin','pass123'),aid)
 def test_16_payment_cannot_auto_approve(self):
  bid=self.book();c.add_slip(self.uid,bid,'x.png',12000)
  c.sweep()
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'UNDER_REVIEW')
 def test_17_coupon_daytime(self):
  start=self.start.replace(hour=13)
  with c.connect() as conn:q=c.quote(conn,1,c.dstr(start),1)
  self.assertEqual(q['total_sat'],9000)
 def test_18_login_bruteforce_throttle(self):
  for _ in range(5):
   with self.assertRaises(c.RuleError):c.login('test@example.org','WRONG')
  with self.assertRaises(c.RuleError) as ctx:c.login('test@example.org','p@ss x')
  self.assertIn('15 นาที',str(ctx.exception))
 def test_19_points_on_approval_once(self):
  aid=self.admin();bid=self.book()
  c.add_slip(self.uid,bid,'test.png',12000)
  with c.connect() as conn:sid=conn.execute('SELECT slip_id FROM payment_slips').fetchone()[0]
  c.review_slip(aid,sid,True)
  with c.connect() as conn:self.assertEqual(c.membership(conn,self.uid)['points'],12)
  with self.assertRaises(c.RuleError):c.review_slip(aid,sid,True)
  with c.connect() as conn:self.assertEqual(c.membership(conn,self.uid)['points'],12)
 def test_20_coupon_member_tier(self):
  bid=self.book()
  with c.connect(True) as conn:
   conn.execute("INSERT INTO points_ledger(user_id,booking_id,points) VALUES(?,?,1000)",(self.uid,bid))
  start=self.start.replace(hour=18)
  with c.connect() as conn:q=c.quote(conn,2,c.dstr(start),1,user_id=self.uid)
  self.assertEqual(q['member_discount_sat'],600)
 def test_21_session_revocation_on_reset(self):
  token,_=c.create_session(user_id=self.uid)
  self.assertIsNotNone(c.get_session(token))
  sent=[];c.request_reset('test@example.org',send=lambda addr,code:sent.append(code))
  c.reset_password('test@example.org',sent[0],'123456','123456')
  self.assertIsNone(c.get_session(token))
 def test_18_upload_wrong_account(self):
  bid=self.book();uid2=c.register('คนที่สอง','two@example.org','0801234567','abcdef')
  with self.assertRaises(c.RuleError):c.add_slip(uid2,bid,'fake.png',12000)
if __name__=='__main__':unittest.main()
