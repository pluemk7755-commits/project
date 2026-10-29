"""Non-destructive template and asset tests for accessible 2-step booking UX."""
import unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]

class BookingUiDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=(BASE/'webapp/server.py').read_text(encoding='utf-8')
        cls.css=(BASE/'static/style.css').read_text(encoding='utf-8')
        cls.js=(BASE/'static/app.js').read_text(encoding='utf-8')

    def test_two_step_booking_form_keeps_backend_route(self):
        for text in ('data-booking-step="1"','data-booking-step="2"','data-booking-next','data-booking-back','action="/book"','data-quote-form'):
            with self.subTest(text=text):self.assertIn(text,self.server)

    def test_booking_fields_and_optional_features_preserved(self):
        for text in ('name="date"','name="time"','name="hours"','name="cue_ids"','name="redeem_points"','name="credit"','<details class="booking-extras">'):
            with self.subTest(text=text):self.assertIn(text,self.server)

    def test_calendar_has_month_year_selection_and_confirm(self):
        for text in ('cal-month','cal-year','cal-confirm','ปฏิทินเลือกวันที่เล่น','aria-pressed','selected>=dateField.min','Escape'):
            with self.subTest(text=text):self.assertIn(text,self.js)

    def test_fonts_thai_and_english(self):
        self.assertIn('family=Kanit',self.css)
        self.assertIn('family=Hind+Siliguri',self.css)
        self.assertIn('Kanit',self.css)

    def test_booking_summary_stays_live(self):
        self.assertIn('/api/quote?',self.js)
        self.assertIn('data-quote-panel',self.server)
        self.assertIn('หลังยืนยันการชำระ',self.js)
