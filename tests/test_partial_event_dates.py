"""Evidence with only a year/month must not fabricate a calendar day."""
import tempfile
import unittest
from pathlib import Path
from goldfish.engine import MemoryDB
from goldfish.consolidate import consolidate_session


class PartialDateTests(unittest.TestCase):
    def test_extraction_preserves_real_event_date_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryDB(Path(tmp) / 'm.db')
            try:
                mid = db.add_message('s', 'user', '2024年开始做展签，2025年8月换了项目。')
                class Model:
                    def extract(self, messages, related):
                        return {'summaries': [
                            {'ref': 'n1', 'text': '2024年开始做展签', 'evidence_ids': [mid], 'start_time': '2024'},
                            {'ref': 'n2', 'text': '2025年8月换项目', 'evidence_ids': [mid], 'start_time': '2025-08'},
                        ], 'links': [], 'profiles': []}
                self.assertEqual(consolidate_session(db, 's', Model())['processed'], 1)
                dates = {row[0] for row in db.db.execute('SELECT start_time FROM summaries')}
                self.assertEqual(dates, {'2024', '2025-08'})
            finally: db.close()

    def test_invalid_partial_month_still_rejects_entire_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryDB(Path(tmp) / 'm.db')
            try:
                mid = db.add_message('s', 'user', '不确定具体日期')
                class Model:
                    def extract(self, messages, related):
                        return {'summaries': [{'ref': 'n1', 'text': '某经历', 'evidence_ids': [mid], 'start_time': '2025-13'}], 'links': [], 'profiles': []}
                with self.assertRaises(ValueError): consolidate_session(db, 's', Model())
                self.assertEqual(db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 0)
            finally: db.close()


if __name__ == '__main__': unittest.main()
