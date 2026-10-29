import tempfile
import unittest
from pathlib import Path
from webapp import core

class TestBalancedCuePrices(unittest.TestCase):
    def test_price_catalog_and_repeat_startup(self):
        tmp=tempfile.TemporaryDirectory(prefix='rngd_price_test_')
        old=core.DATA,core.DB_PATH,core.UPLOAD_DIR
        try:
            core.DATA=Path(tmp.name)
            core.DB_PATH=core.DATA/'test.sqlite3'
            core.UPLOAD_DIR=core.DATA/'uploads'
            core.init_db()
            expected={(brand,model):price for brand,model,price,_ in core._CANONICAL_CUES}
            with core.connect() as c:
                rows=c.execute("SELECT brand,model,price_sat FROM cues WHERE status='AVAILABLE'").fetchall()
                self.assertEqual({(r['brand'],r['model']):r['price_sat'] for r in rows},expected)
                self.assertEqual(len(rows),10)
            with core.connect(True) as c:
                c.execute("UPDATE cues SET price_sat=? WHERE brand='Predator' AND model='REVO 12.4'",(22222,))
            core.init_db()
            with core.connect() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM cues WHERE status='AVAILABLE'").fetchone()[0],10)
                self.assertEqual(c.execute("SELECT price_sat FROM cues WHERE brand='Predator' AND model='REVO 12.4'").fetchone()[0],22222)
        finally:
            core.DATA,core.DB_PATH,core.UPLOAD_DIR=old
            tmp.cleanup()
