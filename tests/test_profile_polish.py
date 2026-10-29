import tempfile,unittest
from pathlib import Path
from webapp import core as db

class ProfileAndWalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.orig=(db.DATA,db.DB_PATH,db.UPLOAD_DIR)
        db.DATA=Path(self.tmp.name);db.DB_PATH=db.DATA/'test.db';db.UPLOAD_DIR=db.DATA/'uploads'
        db.init_db();self.uid=db.register('ชื่อ เดิม','profile@example.org','0812229911','abc123')
    def tearDown(self):
        db.DATA,db.DB_PATH,db.UPLOAD_DIR=self.orig
        self.tmp.cleanup()
    def test_profile_update_with_current_password(self):
        db.update_profile(self.uid,'ชื่อ ใหม่','profile@example.org','0899998888','abc123')
        profile=db.profile_details(self.uid)
        self.assertEqual(profile['full_name'],'ชื่อ ใหม่')
        self.assertEqual(profile['phone'],'0899998888')
        self.assertEqual(db.login('profile@example.org','abc123'),self.uid)
    def test_incorrect_password_keeps_profile(self):
        with self.assertRaises(db.RuleError):
            db.update_profile(self.uid,'ผิด','profile@example.org','0899998888','wrong')
        self.assertEqual(db.profile_details(self.uid)['full_name'],'ชื่อ เดิม')
    def test_email_cannot_change_without_verification(self):
        with self.assertRaises(db.RuleError):
            db.update_profile(self.uid,'ชื่อ ใหม่','other@example.org','0899998888','abc123')
        self.assertEqual(db.profile_details(self.uid)['email'],'profile@example.org')
if __name__=='__main__':unittest.main()
