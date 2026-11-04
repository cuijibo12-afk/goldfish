import json
import tempfile
import unittest
from pathlib import Path
from goldfish.engine import MemoryDB
from goldfish.semantic import validate_coverage
from goldfish.relations import review_relations

class SemanticQualityTests(unittest.TestCase):
    def test_full_requires_whole_memory_and_real_context_quotes(self):
        text='2025年用户去了岛城；红色雨伞在车站找回。';ctx='In 2025 the user visited Daocheng. The red umbrella was recovered at the station.'
        facts=[{'memory_quote':text,'state':'covered','context_quote':ctx}]
        self.assertEqual(validate_coverage(text,ctx,{'facts':facts})['state'],'full')
        facts[0]['context_quote']='fabricated quote'
        self.assertEqual(validate_coverage(text,ctx,{'facts':facts})['state'],'uncertain')

    def test_partial_only_injects_source_fragments_not_generated_facts(self):
        text='2025年用户去了岛城；红色雨伞在车站找回。';ctx='2025年去岛城。'
        facts=[{'memory_quote':'2025年用户去了岛城；','state':'covered','context_quote':ctx},
               {'memory_quote':'红色雨伞在车站找回。','state':'missing','context_quote':''}]
        result=validate_coverage(text,ctx,{'facts':facts})
        self.assertEqual(result['state'],'partial');self.assertEqual(result['text'],'红色雨伞在车站找回。')

    def test_omitting_a_negation_or_detail_cannot_suppress_memory(self):
        memory='用户不想辞职，但同意试两周。'
        f=[{'memory_quote':'同意试两周。','state':'covered','context_quote':'同意试两周。'}]
        self.assertEqual(validate_coverage(memory,'同意试两周。',{'facts':f})['state'],'uncertain')

    def test_ids_alone_and_confidence_do_not_prove_coverage(self):
        r=validate_coverage('用户喜欢咖啡','gf:old-id:v1',{'coverage':'full','confidence':1,'facts':[]})
        self.assertEqual(r['state'],'uncertain')

    def test_disjoint_event_dates_override_model_same_event_self_rating(self):
        sources={'a':{'id':'a','text':'2024年获赠这只包'},'b':{'id':'b','text':'2026年用同一只包野营'}}
        c={'id':'edge','kind':'same_event','left':{'text':'赠包','start_time':'2024','evidence_ids':['a']},
           'right':{'text':'野营','start_time':'2026','evidence_ids':['b']}}
        class Model:
            def decide(self,*args,**kwargs):
                return {'reviews':[{'id':'edge','status':'supported','kind':'same_event','reason':'同一包',
                    'evidence':[{'message_id':'a','quote':'2024年获赠这只包'},{'message_id':'b','quote':'2026年用同一只包野营'}]}]}, {}, None
        class FixedModel(Model):
            def decide(self,*args,**kwargs):
                output,measurement,_=super().decide(*args,**kwargs);return output,measurement
        rows,_,_=review_relations(FixedModel(),[c],sources)
        self.assertEqual(rows[0]['status'],'rejected')

    def test_unreviewed_and_stale_edges_never_expand_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=MemoryDB(Path(tmp)/'m.db')
            try:
                a=db.add_message('s','user','在工地工作');b=db.add_message('s','user','面对未知有担心')
                x=db.add_summary('在工地工作',[a]);y=db.add_summary('面对未知有担心',[b])
                link=db.add_link(x,y,'similar_context','职业变化')
                self.assertEqual(db.active_reviewed_links(),[])
                db.record_link_review(link,{'status':'supported','kind':'similar_context','reason':'有来源的类似情境','evidence':[{'message_id':a,'quote':'在工地工作'},{'message_id':b,'quote':'面对未知有担心'}]},'test')
                self.assertEqual(len(db.active_reviewed_links()),1)
                db.db.execute('UPDATE summaries SET text=? WHERE id=?',('改过内容',y))
                self.assertEqual(db.active_reviewed_links(),[])
            finally:db.close()

if __name__=='__main__':unittest.main()
