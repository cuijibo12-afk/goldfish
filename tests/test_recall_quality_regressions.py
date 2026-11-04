"""Failures observed in the real Chinese model evaluation, reduced to fixtures."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from goldfish.engine import MemoryDB, year_filter


class QualityRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MemoryDB(Path(self.tmp.name) / 'm.db')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def summary(self, text, when=None):
        mid = self.db.add_message('s', 'user', text)
        return self.db.add_summary(text, [mid], when)

    def test_generic_cause_word_does_not_recall_personal_life_for_volcano(self):
        self.summary('选择保留全职工作，原因是每月工资能安排妈妈复诊')
        self.summary('录音机没声音的原因是电池放反')
        result = self.db.recall('冰岛火山最近喷发的地理原因是什么？')
        self.assertEqual(result['results'], [])
        self.assertEqual(result['injection'], '')

    def test_year_comparison_includes_both_periods_and_excludes_rejected_period(self):
        today = date(2026, 11, 4)
        self.assertEqual(year_filter('今年和去年接单条件变了什么', today), {2025, 2026})
        self.assertEqual(year_filter('去年为什么没接，别拿今年的条件替代', today), {2025})

    def test_unrelated_year_matches_do_not_displace_specific_contract_question(self):
        target = self.summary('去年拒绝插画单，因为对方没给出项目周期和付款保障', '2025-03')
        for index in range(8): self.summary(f'去年第{index}次复诊，膝盖康复预约周三早晨', '2025-09')
        result = self.db.recall('去年为什么拒绝插画单？', today=date(2026, 11, 4), max_items=2)
        self.assertEqual(result['results'][0]['id'], target)

    def test_old_relevant_profile_survives_new_irrelevant_profiles(self):
        mid = self.db.add_message('s', 'user', '我从2023年带路就提前打印路线，家里仍用手机')
        self.db.add_profile('user_profile', '用户带路时提前打印纸本路线，家里仍使用手机', [mid])
        for index in range(20):
            self.db.add_profile('user_profile', f'第{index}份新合同每月结算', [mid])
        result = self.db.recall('带路为什么拿纸，不一直看手机？')
        self.assertIn('打印纸本路线', result['injection'])
        self.assertNotIn('新合同', result['injection'])

    def test_specific_feedback_can_be_recalled_without_global_style_rewrite(self):
        mid = self.db.add_message('s', 'user', '刚才回答机械，只说这一次')
        self.db.add_profile('feedback', '用户反馈刚才回答机械，仅限这一次', [mid])
        self.assertIn('刚才回答机械', self.db.recall('刚才机械是长期不准给清单吗？')['injection'])
        self.assertEqual(self.db.recall('冰岛火山喷发机制')['injection'], '')

    def test_generic_request_for_a_short_answer_does_not_recall_old_feedback(self):
        mid = self.db.add_message('s', 'user', '刚才回答机械，只说这一次')
        self.db.add_profile('feedback', '用户评价助手刚才那次回答太机械，仅针对那一次回答', [mid])
        self.assertEqual(self.db.recall('旧码头的经历里有哪些小细节值得保留？简短回答即可。')['injection'], '')

    def test_specific_expectation_is_not_tied_with_every_newer_expectation(self):
        mid = self.db.add_message('s', 'user', '我希望你不要因为是我就附和，请先给证据')
        self.db.add_profile('interaction_expectation', '用户希望不要附和，要先给证据', [mid])
        for index in range(12):
            self.db.add_profile('interaction_expectation', f'第{index}项医疗问题先问专业人员', [mid])
        text = self.db.recall('别总附和是在描述你的天性吗？')['injection']
        self.assertIn('不要附和', text)
        self.assertNotIn('医疗问题', text)


if __name__ == '__main__': unittest.main()
