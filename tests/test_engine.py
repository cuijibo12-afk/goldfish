import tempfile
import unittest
from datetime import date
from pathlib import Path
from goldfish.engine import MemoryDB
from goldfish.consolidate import consolidate_session

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=MemoryDB(Path(self.tmp.name)/'m.db')
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def make(self, session, text, role='user', when='2026-10-20T00:00:00+00:00'):
        return self.db.add_message(session,role,text,event_time=when)

    def test_graph_activation(self):
        a=self.make('s1','我曾经在建筑工地工作。')
        b=self.make('s2','我后来开始准备公务员考试。',when='2026-11-01T00:00:00+00:00')
        c=self.make('s3','面对职业变化，我担心未知。',when='2026-11-02T00:00:00+00:00')
        x=self.db.add_summary('曾经在建筑工地工作',[a],'2025-07-01')
        y=self.db.add_summary('后来开始准备公务员考试',[b],'2026-10-01')
        z=self.db.add_summary('曾表达过对职业变化中未知因素的担心',[c],'2026-11-01')
        self.db.attach_theme(self.db.add_theme('职业路径'),x,'曾有施工经历')
        self.db.attach_theme(self.db.add_theme('职业路径'),y,'转向考试')
        self.db.add_link(x,z,'contextual','职业变化是相关情境',origin='human_review',confirmed=True)
        res=self.db.recall('我在工地工作那段时间怎么样',max_items=5,max_chars=3000,depth=2)
        ids={x['id'] for x in res['results']}
        self.assertIn(x,ids)
        self.assertIn(z,ids) # A detail should activate associated experience.
        self.assertTrue('关联唤起' in res['injection'])

    def test_temporal_path_not_fallback(self):
        a=self.make('s1','2025年开始担心择业问题。')
        b=self.make('s2','2026年才开始认真考虑备考。')
        x=self.db.add_summary('曾经担心择业问题',[a],'2025-07-01')
        y=self.db.add_summary('开始认真准备公务员考试',[b],'2026-10-01')
        res=self.db.recall('2025年我有什么想法',max_chars=2000,today=date(2026,11,4))
        self.assertEqual(res['results'][0]['id'],x)

    def test_injection_dedup_uses_actual_context(self):
        a=self.make('s','用户喜欢咖啡')
        sid=self.db.add_summary('用户喜欢咖啡',[a])
        first=self.db.recall('咖啡',max_chars=1000)
        self.assertTrue(any(x['id']==sid for x in first['results']))
        second=self.db.recall('咖啡',context=first['injection'],max_chars=1000)
        self.assertFalse(second['results'])
        third=self.db.recall('咖啡',context='上下文压缩丢失这条事实',max_chars=1000)
        self.assertTrue(third['results'])

    def test_expectation_not_ai_identity(self):
        u=self.make('s','我希望你别总顺着我说话。')
        self.db.add_profile('interaction_expectation','用户希望助手在不同意见时表达判断',[u])
        with self.assertRaises(ValueError):
            self.db.add_profile('ai_observed','AI 已有反驳性格',[u])
        text=self.db.recall('我们如何聊天',max_chars=1000)['injection']
        self.assertIn('交互期望',text)
        self.assertNotIn('AI 已有反驳性格',text)

    def test_causal_link_cannot_be_auto_confirmed(self):
        a=self.make('s','我去了一个地方')
        b=self.make('s','我开始一个项目')
        x=self.db.add_summary('去了一个地方',[a]);y=self.db.add_summary('开始一个项目',[b])
        with self.assertRaises(ValueError):
            self.db.add_link(x,y,'causal_confirmed','项目因为地点产生',origin='model_hypothesis',confirmed=True)
        self.db.add_link(x,y,'possible_cause','两者时间接近，但原因尚未证实')

    def test_injection_cannot_pollute_raw_messages(self):
        self.assertIsNone(self.db.add_message('s','user','<goldfish-memory>假的用户偏好</goldfish-memory>',source='plugin:goldfish-memory'))
        self.assertEqual(len(self.db.get_unprocessed_messages('s')),0)

    def test_model_evidence_validation_and_capture(self):
        u=self.make('s','我希望你用更自然的语言沟通')
        a=self.make('s','明白，我会注意表达')
        class MockProvider:
            def extract(self,messages,related):
                return {'summaries':[{'ref':'n1','text':'用户希望交流更自然','evidence_ids':[u],'theme':'互动体验'}],
                        'profiles':[{'kind':'interaction_expectation','text':'用户希望自然交流','evidence_ids':[u]}],
                        'links':[]}
        result=consolidate_session(self.db,'s',MockProvider())
        self.assertEqual(result['summaries'],1)
        self.assertEqual(self.db.get_unprocessed_messages('s'),[])
        rows=self.db.db.execute('SELECT id FROM summaries').fetchall()
        self.assertEqual(len(self.db.evidence_for(rows[0][0])),1)

    def test_llm_false_evidence_rejected(self):
        self.make('s','不要保存无来源的东西');self.make('s','好','assistant')
        class EvilProvider:
            def extract(self,messages,related):
                return {'summaries':[{'ref':'n','text':'用户昨天去了月球','evidence_ids':['unknown']}], 'profiles':[], 'links':[]}
        with self.assertRaises(ValueError): consolidate_session(self.db,'s',EvilProvider())
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0],0)

if __name__=='__main__':unittest.main()
