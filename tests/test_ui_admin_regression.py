import unittest
from pathlib import Path
from webapp import server
from webapp import core as db

class UiAndAdminFixTests(unittest.TestCase):
    def test_admin_checkin_error_returns_to_staff_view(self):
        import inspect
        source=inspect.getsource(server.AppHandler.do_POST)
        self.assertIn("'/admin/checkin','/admin/checkout'",source)
        self.assertIn("'&error='+quote(str(err))",source)
        self.assertIn("view='booking'",source)
    def test_admin_alert_is_displayed_in_admin_page(self):
        import inspect
        self.assertIn("args.get('error')",inspect.getsource(server.AppHandler.admin_page))
    def test_table_assets_exist(self):
        for name in ('table_snooker_premium.webp','table_vip_premium.webp','table_pool_premium.webp'):
            self.assertTrue((server.STAT/'images'/name).is_file())
    def test_cue_assets_exist(self):
        for name in ('cue_predator.png','cue_cuetec.png','cue_mezz.png','cue_peradon.png','cue_aspire.png'):
            self.assertTrue((server.STAT/'images'/name).is_file())
    def test_real_checkin_rule_still_enforces_start(self):
        import inspect
        self.assertIn('current<start',inspect.getsource(db.check_in))
