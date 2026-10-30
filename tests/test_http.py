import os, sys, tempfile, unittest, threading, urllib.request,urllib.parse,urllib.error,http.cookiejar,re
from datetime import timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from webapp import core as c
from webapp.server import ReusableHTTP,AppHandler

class BrowserHttpTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory(prefix='rngd_http_');cls.old=(c.DATA,c.DB_PATH,c.UPLOAD_DIR)
  c.DATA=Path(cls.tmp.name);c.DB_PATH=c.DATA/'rngd.sqlite3';c.UPLOAD_DIR=c.DATA/'uploads'
  os.environ['RNGD_TEST_MODE']='1';os.environ['RNGD_QUIET']='1'
  c.init_db()
  with c.connect(True) as conn:conn.execute("INSERT INTO staff_users(username,password_hash,role) VALUES(?,?,'ADMIN')",('admin_test',c.hash_password('pass123')))
  cls.http=ReusableHTTP(('127.0.0.1',0),AppHandler);cls.port=cls.http.server_port
  cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
 @classmethod
 def tearDownClass(cls):
  cls.http.shutdown();cls.http.server_close();cls.thread.join(timeout=3)
  c.DATA,c.DB_PATH,c.UPLOAD_DIR=cls.old;cls.tmp.cleanup()
  os.environ.pop('RNGD_TEST_MODE',None)
 def open_browser(self):
  self.jar=http.cookiejar.CookieJar();self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
 def httpget(self,path):
  response=self.client.open(f'http://127.0.0.1:{self.port}{path}',timeout=8)
  return response.read().decode('utf-8')
 def post(self,path,fields):
  form=urllib.parse.urlencode({'csrf':self.token,**fields}).encode()
  return self.client.open(urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',data=form,method='POST'),timeout=8)
 def extract(self,html):
  token=re.search(r'name="csrf" value="([^"]+)"',html)
  self.assertIsNotNone(token)
  self.token=token.group(1)
 def test_01_front_pages(self):
  self.open_browser()
  for path,word in [('/','จำนวนโต๊ะว่างตอนนี้'),('/tables','เลือกโต๊ะ'),('/cues','ไม้คิวพรีเมียม'),('/register','สมัครสมาชิก'),('/forgot','ลืมรหัสผ่าน')]:
   text=self.httpget(path);self.assertIn(word,text)
  self.assertEqual(self.httpget('/tables').count('class="table-card"'),9)
 def test_01b_static_assets_load_without_a_session(self):
  # A fresh browser opens CSS / JS / images before an auth session is created.
  self.open_browser()
  expected=[
   ('/static/style.css','text/css',b'.page'),
   ('/static/app.js','text/javascript',b'document'),
   ('/static/images/snooker_red_ball.png','image/png',b'\x89PNG\r\n\x1a\n'),
   ('/static/images/home_hero.png','image/png',b'\x89PNG\r\n\x1a\n'),
  ]
  for route,mimetype,substring in expected:
   with self.subTest(route=route):
    with self.client.open(f'http://127.0.0.1:{self.port}{route}',timeout=8) as resp:
     self.assertEqual(resp.status,200)
     self.assertIn(mimetype,resp.headers.get('Content-Type',''))
     data=resp.read()
     self.assertGreater(len(data),50)
     self.assertIn(substring,data)
  # Static resources should not create a login session cookie on their own.
  self.assertFalse(any(cookie.name=='rngd_session' for cookie in self.jar))
 def test_01c_static_assets_load_after_home_page(self):
  self.open_browser()
  html=self.httpget('/')
  self.assertIn('href="/static/style.css"',html)
  self.assertIn('src="/static/app.js"',html)
  self.assertIn('/static/images/snooker_red_ball.png',html)
  with self.client.open(f'http://127.0.0.1:{self.port}/static/style.css',timeout=8) as response:
   self.assertEqual(response.status,200)
   self.assertIn(b'.page',response.read())
 def test_01d_missing_static_asset_returns_404(self):
  self.open_browser()
  with self.assertRaises(urllib.error.HTTPError) as err:
   self.httpget('/static/images/not-exist.png')
  self.assertEqual(err.exception.code,404)
 def test_01e_forgot_page_renders_turnstile_for_central_otp(self):
  self.open_browser();html=self.httpget('/forgot')
  self.assertIn('data-otp-turnstile',html)
  self.assertIn('data-otp-forgot-form',html)
  self.assertIn('name="cf-turnstile-response"',html)
  self.assertIn('0x4AAAAAAFqixMTMFSq4YmK2',html)
 def test_02_register_login_booking_and_receipt(self):
  self.open_browser();self.extract(self.httpget('/register'))
  self.post('/register',{'name':'ลูกค้าทดสอบเว็บ','email':'webuser@example.org','phone':'0855559999','password':'abcdef'}).read()
  self.assertIn('รายการของฉัน',self.httpget('/tables'))
  html=self.httpget('/book?table=1');self.extract(html)
  start=(c.now()+timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)
  self.post('/book',{'table_id':'1','start':start.strftime('%Y-%m-%dT%H:%M'),'hours':'1','cue_id':'','credit':'0'}).read()
  self.assertIn('ส่งสลิป',self.httpget('/slip?id=1'))
  # Server's receipt must be inaccessible while still unpaid.
  try:self.httpget('/receipt?id=1')
  except urllib.error.HTTPError as exc:self.assertEqual(exc.code,403)
  else:self.fail('Unpaid receipt should not be available')
 def test_02b_real_http_multipart_review_and_receipt(self):
  self.open_browser();self.extract(self.httpget('/register'))
  self.post('/register',{'name':'ลูกค้าทดสอบสลิป','email':'slipuser@example.org','phone':'0833332222','password':'abcdef'}).read()
  self.extract(self.httpget('/book?table=2'))
  start=(c.now()+timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)
  self.post('/book',{'table_id':'2','start':start.strftime('%Y-%m-%dT%H:%M'),'hours':'1','cue_id':'','credit':'0'}).read()
  with c.connect() as conn:bid=conn.execute('SELECT max(booking_id) FROM bookings').fetchone()[0]
  self.extract(self.httpget(f'/slip?id={bid}'))
  boundary='----RNGDTEST'+os.urandom(8).hex()
  blob=b''
  for key,value in [('csrf',self.token),('id',str(bid))]:
   blob+=(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode().replace(b'\r',b'\r').replace(b'\n',b'\n')
  png=(Path(__file__).resolve().parents[1]/'tools'/'SAMPLE_SLIP_NOT_REAL.png').read_bytes()
  blob+=(f'--{boundary}\r\nContent-Disposition: form-data; name="slip"; filename="sample.png"\r\nContent-Type: image/png\r\n\r\n').encode()+png+b'\r\n'+f'--{boundary}--\r\n'.encode()
  req=urllib.request.Request(f'http://127.0.0.1:{self.port}/slip',data=blob,method='POST',headers={'Content-Type':'multipart/form-data; boundary='+boundary})
  self.client.open(req,timeout=8).read()
  with c.connect() as conn:
   slip=conn.execute('SELECT * FROM payment_slips WHERE booking_id=?',(bid,)).fetchone()
   self.assertIsNotNone(slip);self.assertEqual(slip['status'],'PENDING')
  with self.assertRaises(urllib.error.HTTPError):self.httpget(f'/receipt?id={bid}')
  # Separate administrator browser with its own login cookie.
  self.open_browser();self.extract(self.httpget('/staff/login'))
  self.post('/staff/login',{'identifier':'admin_test','password':'pass123'}).read()
  self.extract(self.httpget('/admin?view=payments'))
  self.post('/admin/review',{'slip_id':str(slip['slip_id']),'decision':'approve'}).read()
  with c.connect() as conn:self.assertEqual(conn.execute('SELECT status FROM bookings WHERE booking_id=?',(bid,)).fetchone()[0],'CONFIRMED')
 def test_03_bad_csrf_rejected(self):
  self.open_browser();self.httpget('/register');self.token='INVALID'
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.post('/register',{'name':'a','email':'bad@example.org','phone':'0810000000','password':'abcdef'})
  self.assertEqual(ctx.exception.code,400)
 def test_04_admin_page_restricted(self):
  self.open_browser()
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.httpget('/admin')
  self.assertEqual(ctx.exception.code,403)
 def test_05_admin_login(self):
  self.open_browser();self.extract(self.httpget('/staff/login'))
  self.post('/staff/login',{'identifier':'admin_test','password':'pass123'}).read()
  self.assertIn('ศูนย์จัดการร้าน',self.httpget('/admin'))
  self.assertIn('จัดการสมาชิก',self.httpget('/admin/users'))
 def test_05b_failed_admin_login_shows_message_and_allows_retry(self):
  self.open_browser();self.extract(self.httpget('/staff/login'))
  response=self.post('/staff/login',{'identifier':'not_an_admin','password':'wrongpass'})
  html=response.read().decode('utf-8')
  self.assertEqual(urllib.parse.urlparse(response.geturl()).path,'/staff/login')
  self.assertIn('เข้าสู่ระบบผู้ดูแล',html)
  self.assertIn('เข้าสู่ระบบไม่สำเร็จ',html)
  self.assertNotIn('กลับไปหน้าเข้าสู่ระบบ ADMIN',html)
  self.assertIn('action="/staff/login"',html)
  self.assertIn('name="identifier"',html)
  self.assertIn('name="password"',html)
  # The same browser session can correct the credentials and sign in normally.
  self.extract(html)
  retry=self.post('/staff/login',{'identifier':'admin_test','password':'pass123'})
  retry_html=retry.read().decode('utf-8')
  self.assertEqual(urllib.parse.urlparse(retry.geturl()).path,'/admin')
  self.assertIn('ศูนย์จัดการร้าน',retry_html)
 def test_06_admin_filter_pages_render_and_keep_selection(self):
  self.open_browser();self.extract(self.httpget('/staff/login'))
  self.post('/staff/login',{'identifier':'admin_test','password':'pass123'}).read()
  for path in ('/admin?view=payments&status=APPROVED',
               '/admin?view=payments&status=REJECTED',
               '/admin?view=booking&status=PENDING_SLIP',
               '/admin?view=booking&status=UNDER_REVIEW',
               '/admin?view=booking&search_by=booking_id&q=1'):
   with self.subTest(path=path):
    html=self.httpget(path)
    self.assertIn('class="admin-filters admin-search-clear"',html)
    self.assertIn('class="table-scroll"',html)
    self.assertNotIn('Traceback',html)
  html=self.httpget('/admin?view=payments&status=APPROVED')
  self.assertIn('value="APPROVED" selected',html)
  html=self.httpget('/admin?view=booking&status=UNDER_REVIEW')
  self.assertIn('value="UNDER_REVIEW" selected',html)
if __name__=='__main__':unittest.main()
