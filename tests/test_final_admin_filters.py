"""Regression checks for exact admin search, state isolation and paging.
Each test uses an isolated temporary database; no customer data is loaded.
"""
import tempfile
import unittest
from pathlib import Path
from webapp import core
from webapp.server import admin_search_rows

class FinalAdminFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rngd_final_filters_')
        self.old=(core.DATA,core.DB_PATH,core.UPLOAD_DIR)
        core.DATA=Path(self.tmp.name)
        core.DB_PATH=core.DATA/'test.sqlite3'
        core.UPLOAD_DIR=core.DATA/'uploads'
        core.init_db()
        self.uid=core.register('สมาชิก ทดสอบ','filters@example.test','0812341122','secret11')
        with core.connect(True) as c:
            for i in range(1,526):
                state=('PENDING_SLIP' if i%3==1 else 'UNDER_REVIEW' if i%3==2 else 'CONFIRMED')
                c.execute('''INSERT INTO bookings(booking_id,user_id,table_id,start_at,end_at,status,
                             table_fee_sat,total_sat,due_sat,hold_expires_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?)''',
                          (i,self.uid,(i%3)+1,'2026-11-01 12:00:00','2026-11-01 13:00:00',
                           state,12000,12000,12000,'2026-11-01 11:00:00'))
            for i,state in [(1,'PENDING'),(2,'APPROVED'),(3,'REJECTED'),(4,'REPLACED')]:
                c.execute('''INSERT INTO payment_slips(slip_id,booking_id,user_id,file_name,
                             amount_sat,status,uploaded_at) VALUES(?,?,?,?,?,?,?)''',
                          (i,i,self.uid,f'{i}.png',12000,state,'2026-10-27 12:00:00'))
    def tearDown(self):
        core.DATA,core.DB_PATH,core.UPLOAD_DIR=self.old
        self.tmp.cleanup()

    def test_search_old_booking_beyond_first_500(self):
        rows,count,page,*_=admin_search_rows('booking',{'search_by':'booking_id','q':'1'})
        self.assertEqual((count,page), (1,1))
        self.assertEqual([r['booking_id'] for r in rows],[1])

    def test_pagination_and_bounds(self):
        rows,count,page,*_=admin_search_rows('booking',{'page':'1'})
        self.assertEqual((len(rows),count,page),(100,525,1))
        self.assertEqual(rows[0]['booking_id'],525)
        rows,count,page,*_=admin_search_rows('booking',{'page':'6'})
        self.assertEqual((len(rows),count,page),(25,525,6))
        self.assertEqual(rows[-1]['booking_id'],1)
        rows,count,page,*_=admin_search_rows('booking',{'page':'99999'})
        self.assertEqual(page,6)
        rows,count,page,*_=admin_search_rows('booking',{'page':'not-a-page'})
        self.assertEqual(page,1)

    def test_booking_state_filters_are_true_db_states(self):
        for state in ('PENDING_SLIP','UNDER_REVIEW','CONFIRMED'):
            rows,total,_,_,chosen=admin_search_rows('booking',{'status':state})
            self.assertEqual(chosen,state)
            self.assertGreater(total,0)
            self.assertTrue(all(r['status']==state for r in rows))
        rows,_,_,_,chosen=admin_search_rows('booking',{'status':'PENDING'})
        self.assertEqual(chosen,'PENDING_SLIP')
        self.assertTrue(all(r['status']=='PENDING_SLIP' for r in rows))

    def test_slip_states_are_not_overwritten_by_booking_states(self):
        for state,sid in [('PENDING',1),('APPROVED',2),('REJECTED',3),('REPLACED',4)]:
            rows,total,_,_,chosen=admin_search_rows('payments',{'status':state})
            self.assertEqual((total,chosen),(1,state))
            self.assertEqual(rows[0]['slip_id'],sid)

    def test_search_type_does_not_cross_match(self):
        rows,n,*_=admin_search_rows('payments',{'search_by':'slip_id','q':'1'})
        self.assertEqual((n,[r['slip_id'] for r in rows]),(1,[1]))
        rows,n,*_=admin_search_rows('payments',{'search_by':'booking_id','q':'1'})
        self.assertEqual((n,[r['booking_id'] for r in rows]),(1,[1]))
        rows,n,*_=admin_search_rows('booking',{'search_by':'table','q':'1'})
        self.assertGreater(n,0)
        self.assertTrue(all(r['table_code'].endswith('01') for r in rows))
        rows,n,*_=admin_search_rows('booking',{'search_by':'booking_id','q':'%_1'})
        self.assertEqual(n,0)

    def test_customer_search_treats_sql_wildcards_literally(self):
        self.assertEqual(admin_search_rows('booking',{'search_by':'customer','q':'%'})[1],0)
        self.assertEqual(admin_search_rows('booking',{'search_by':'customer','q':'_'})[1],0)
        self.assertEqual(admin_search_rows('booking',{'search_by':'customer','q':'สมาชิก'})[1],525)

    def test_unique_name_login_and_duplicate_requires_phone(self):
        self.assertEqual(core.login('สมาชิก ทดสอบ','secret11'),self.uid)
        self.assertEqual(core.login('filters@example.test','secret11'),self.uid)
        core.register('สมาชิก ทดสอบ','filters2@example.test','0812341123','secret22')
        with self.assertRaises(core.RuleError) as cm:
            core.login('สมาชิก ทดสอบ','secret11')
        self.assertIn('ซ้ำ',str(cm.exception))
        self.assertEqual(core.login('0812341122','secret11'),self.uid)

if __name__=='__main__': unittest.main()
