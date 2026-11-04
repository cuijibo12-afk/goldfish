import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goldfish.consolidate import consolidate_session
from goldfish.engine import MemoryDB
from goldfish.providers import EmbeddingModel,ModelConfig
from goldfish.retrieval import candidate_pool,profile_candidates
from goldfish.semantic import plan_recall
from goldfish.server import Service


class HybridIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=MemoryDB(Path(self.temp.name)/'m.db')
    def tearDown(self):
        self.db.close();self.temp.cleanup()
    def summary(self,text):
        mid=self.db.add_message('s','user',text)
        return self.db.add_summary(text,[mid]),mid
    def test_updated_and_racing_index_never_reuses_old_vector(self):
        sid,mid=self.summary('借出红色雨伞')
        snap=self.db.missing_embeddings('m')
        self.assertEqual(self.db.store_embeddings('m',[(sid,[1,0])],snapshots=snap),1)
        self.assertIn(sid,self.db.semantic_scores([1,0],'m'))
        self.db.update_summary(sid,'借出蓝色雨伞',[mid])
        self.assertEqual(self.db.semantic_scores([1,0],'m'),{})
        self.assertEqual(self.db.store_embeddings('m',[(sid,[1,0])],snapshots=snap),0)
        current=self.db.missing_embeddings('m')
        self.assertEqual(current[0]['version'],2)
        self.assertEqual(self.db.store_embeddings('m',[(sid,[0,1])],snapshots=current),1)
        self.assertIn(sid,self.db.semantic_scores([0,1],'m'))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summary_revisions').fetchone()[0],1)
        self.db.archive_summary(sid)
        self.assertEqual(self.db.semantic_scores([0,1],'m'),{})
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summary_vectors').fetchone()[0],0)
        self.assertEqual(self.db.get_message(mid)['text'],'借出红色雨伞')
    def test_external_change_invalidates_cached_vectors_and_model_switch_rebuilds(self):
        sid,mid=self.summary('杯套A731')
        self.db.store_embeddings('m',[(sid,[1,0])]);self.db.semantic_scores([1,0],'m')
        other=MemoryDB(self.db.path)
        try:other.db.execute('UPDATE summaries SET text=? WHERE id=?',('杯套B742',sid))
        finally:other.close()
        self.assertNotIn(sid,self.db.semantic_scores([1,0],'m'))
        self.assertEqual(len(self.db.missing_embeddings('m')),1)
        self.db.store_embeddings('m',[(sid,[1,0])])
        self.assertEqual(len(self.db.missing_embeddings('new-model')),1)
        self.assertEqual(self.db.semantic_scores([1,0],'new-model'),{})
    def test_bad_profile_and_dependent_edge_do_not_rollback_valid_facts(self):
        sid,mid=self.summary('旧事实')
        assistant=self.db.add_message('batch','assistant','助手承诺，不是用户期望')
        user=self.db.add_message('batch','user','用户明确叙述接送家人')
        class Model:
            def extract(self,*args):
                return {'summaries':[{'ref':'ok','text':'接送家人','evidence_ids':[user]},
                    {'ref':'bad','text':'错误概括','evidence_ids':['unknown']}],
                    'profiles':[{'kind':'interaction_expectation','text':'捏造用户期望','evidence_ids':[assistant]}],
                    'links':[{'from':'ok','to':'bad','kind':'same_event','reason':'错误依赖'}]}
        out=consolidate_session(self.db,'batch',Model())
        self.assertEqual(out['summaries'],1);self.assertEqual(out['profiles'],0);self.assertEqual(out['links'],0)
        self.assertEqual(len(out['rejected_items']),3)
        self.assertEqual(self.db.get_unprocessed_messages('batch'),[])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM consolidation_rejections').fetchone()[0],3)
    def test_ai_behavior_cannot_be_removed_by_many_newer_profiles(self):
        a=self.db.add_message('s','assistant','本次按用户更正把4320更新为3840')
        pid=self.db.add_profile('ai_observed','本次实际更正预算4320为3840',[a])
        for i in range(70):
            mid=self.db.add_message('s','user','新爱好'+str(i));self.db.add_profile('user_profile','新爱好'+str(i),[mid])
        rows=profile_candidates(self.db,'你自称冷静毒舌，哪些有行为证据？')
        self.assertEqual(rows[0]['id'],'profile:'+pid)
    def test_four_channels_and_graph_cannot_replace_exact_fact(self):
        a,_=self.summary('2025年蓝色LT-42借给李明');b,_=self.summary('无关的植物')
        self.db.db.execute('UPDATE summaries SET start_time=? WHERE id=?',('2025-04-06',a))
        tid=self.db.add_theme('借物');self.db.attach_theme(tid,a)
        self.db.add_link(a,b,'same_topic','人为测试边',origin='human_review',confirmed=True)
        pool=candidate_pool(self.db,'2025年4月6日借物LT-42',semantic={a:.7,b:.95},limit=1,depth=2)
        self.assertEqual(pool['summaries'][0]['id'],a)
        self.assertEqual(set(pool['channels']),{'embedding','keyword','theme','time'})
    def test_only_whole_quote_checked_duplicate_can_be_suppressed(self):
        sid,mid=self.summary('用户希望缺价时留空，不编报价。')
        pid=self.db.add_profile('interaction_expectation','用户希望缺价时留空，不编报价。',[mid])
        class Model:
            def decide(self,prompt,data,**kwargs):
                first={'id':'profile:'+pid,'why':'交互期望','facts':[]}
                second={'id':sid,'why':'重复','facts':[],'duplicate_of':'profile:'+pid,
                    'duplicate_facts':[{'memory_quote':'用户希望缺价时留空，不编报价。','state':'covered','context_quote':'用户希望缺价时留空，不编报价。'}]}
                return {'intent':'fact','selected':[first,second]},{}
        plan=plan_recall(self.db,Model(),'我希望缺价时怎么做')
        self.assertEqual(plan['selected'][1]['state'],'redundant')
        result=self.db.recall('我希望缺价时怎么做',semantic_plan=plan,max_chars=1200)
        self.assertEqual(len(result['results']),0);self.assertEqual(result['profile_ids'],[pid])
    def test_embedding_revision_prefix_and_usage_are_independent(self):
        cfg=ModelConfig('EMBED','openai','http://localhost:18321','bge',revision='sha',query_prefix='查询：')
        model=EmbeddingModel(cfg)
        with patch('goldfish.providers._post_json',return_value={'data':[{'index':0,'embedding':[3,4]}],'usage':{'prompt_tokens':8,'total_tokens':8}}) as post:
            self.assertEqual(model.embed(['问题'],purpose='query'),[[.6,.8]])
            self.assertEqual(post.call_args.args[1]['input'],['查询：问题'])
        self.assertEqual(model.last_call['usage']['total_tokens'],8)
        self.assertNotEqual(cfg.identity,ModelConfig('EMBED','openai','http://localhost:18321','bge',revision='other').identity)

    def test_auxiliary_update_and_delete_cannot_retrieve_stale_profile(self):
        mid=self.db.add_message('s','user','希望先列缺项')
        pid=self.db.add_profile('interaction_expectation','先列缺项',[mid])
        snap=self.db.missing_aux_embeddings('m')
        self.db.store_aux_embeddings('m',[[1,0]],snap)
        self.assertIn('profile:'+pid,self.db.auxiliary_scores([1,0],'m'))
        self.db.db.execute('UPDATE profile SET text=? WHERE id=?',('先说明证据',pid))
        self.assertEqual(self.db.auxiliary_scores([1,0],'m'),{})
        self.assertEqual(len(self.db.missing_aux_embeddings('m')),1)
        self.db.db.execute("UPDATE profile SET status='archived' WHERE id=?",(pid,))
        self.db.prune_vector_index()
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM auxiliary_vectors').fetchone()[0],0)

    def test_model_selection_reason_is_not_injected_as_personality_evidence(self):
        sid,mid=self.summary('本次把预算4320更正为3840')
        plan={'intent':'fact','selected':[{'id':sid,'text':'本次把预算4320更正为3840','state':'missing',
            'why':'证明AI天生冷静，而且用户在寻找控制感'}]}
        result=self.db.recall('实际纠错证据',semantic_plan=plan,max_chars=1200)
        self.assertIn('3840',result['injection'])
        self.assertNotIn('天生冷静',result['injection']);self.assertNotIn('控制感',result['injection'])
        self.assertIn('天生冷静',result['results'][0]['selection_reason_model'])

    def test_exact_query_cache_is_bounded_and_revision_specific(self):
        class Embeddings:
            identity='revision1';last_call={'usage':{'prompt_tokens':2}}
            def __init__(self):self.calls=0
            def embed(self,texts,**kwargs):self.calls+=1;return [[1,0]]
        with patch.dict(os.environ,{},clear=True):service=Service(Path(self.temp.name)/'cache.db')
        try:
            service.embeddings=Embeddings()
            self.assertFalse(service._query_embedding('q',1)[1])
            self.assertTrue(service._query_embedding('q',1)[1])
            self.assertEqual(service.embeddings.calls,1)
            service.embeddings.identity='revision2'
            self.assertFalse(service._query_embedding('q',1)[1])
            for i in range(130):service._query_embedding(str(i),1)
            self.assertEqual(len(service._query_vectors),128)
        finally:service.close()

    def test_manual_rebuild_continues_past_one_bounded_batch_in_background(self):
        class Embeddings:
            identity='test';configured=True
            def embed(self,texts,**kwargs):return [[1,0] for text in texts]
        with patch.dict(os.environ,{},clear=True):service=Service(Path(self.temp.name)/'rebuild.db')
        try:
            service.embeddings=Embeddings()
            with service.db.transaction():
                for i in range(270):
                    mid=service.db.add_message('s','user','记录'+str(i));service.db.add_summary('记录'+str(i),[mid])
            result=service.handle('/reindex',{'force':True})
            self.assertEqual(result['indexed'],256)
            service.pool.submit(lambda:None).result(timeout=3)
            self.assertEqual(service.db.missing_embeddings('test'),[])
        finally:service.close()


if __name__=='__main__':unittest.main()
