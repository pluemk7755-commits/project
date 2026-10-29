"""HTTP QR delivery and password-reset-page gate on an isolated fake shop."""
import json,os,sys,tempfile,threading,unittest,urllib.request,urllib.error,http.cookiejar
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from webapp import core as db
from webapp.server import AppHandler,ReusableHTTP

class PayeeAndResetHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='rngd_payee_test_')
        cls.old=(db.DATA,db.DB_PATH,db.UPLOAD_DIR)
        db.DATA=Path(cls.tmp.name);db.DB_PATH=db.DATA/'db.sqlite3';db.UPLOAD_DIR=db.DATA/'uploads'
        db.init_db()
        (db.DATA/'payee_config.json').write_text(json.dumps({'shop':'TEST SHOP','bank':'FAKE','name':'TEST USER','account':'0000000000'}))
        Image.new('RGB',(24,24),'white').save(db.DATA/'payee_qr.png')
        cls.http=ReusableHTTP(('127.0.0.1',0),AppHandler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join(timeout=3)
        db.DATA,db.DB_PATH,db.UPLOAD_DIR=cls.old
        cls.tmp.cleanup()
    def test_static_merchant_qr_is_valid_png(self):
        with urllib.request.urlopen(self.base+'/payee-qr') as resp:
            self.assertIn('image/png',resp.headers['Content-Type'])
            self.assertTrue(resp.read().startswith(b'\x89PNG'))
    def test_reset_page_not_accessible_before_otp(self):
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        resp=opener.open(self.base+'/reset')
        self.assertEqual(resp.url,self.base+'/forgot')
        self.assertIn('ลืมรหัสผ่าน',resp.read().decode('utf-8'))
if __name__=='__main__':unittest.main()
