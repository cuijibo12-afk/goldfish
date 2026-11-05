"""Synthetic plans/reviews test deterministic boundaries, not actual model quality."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from goldfish.engine import MemoryDB
from goldfish import compaction as d, events, queries


class EventMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=MemoryDB(Path(self.temp.name)/'test.sqlite3')
        d.migrate(self.db); events.migrate(self.db)
        self.counter=0

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def prepare(self,texts):
        self.counter+=1
        sources=[]
        for index,text in enumerate(texts):
            src={'namespace':'n','session_id':'s','event_seq':self.counter*10+index,
                 'message_id':f'u{self.counter}-{index}','event_time':'2026-11-05T00:00:00Z',
                 'role':'user','source_kind':'user','text':text}
            sources.append({'source':src,'start':0,'end':len(text)})
        manifest={'version':1,'authority':'host_compaction_opt_in/v1','namespace':'n','session_id':'s',
                  'compaction_id':str(self.counter),'target':{'provider':'p','model':'main'},'sources':sources}
        job=d.enqueue(self.db,manifest)['job_id']
        return job,d.prepare(self.db,job)

    def fact(self,quote,alias='u0',**change):
        return {'ref':'a1','text':quote,'aspect':'process','time':'2022','operation':'new',
                'target_fact':None,'evidence':[{'source':alias,'quote':quote}],**change}

    def group(self,facts,identity='澄湾-A81',ref='e1',existing=None):
        return {'ref':ref,'existing':existing,'event_key':identity,'title':'E验收虚构：'+identity,
                'theme':'行程准备','anchors':[{'source':'u0','quote':facts[0]['evidence'][0]['quote']}],'facts':facts}

    def commit(self,job,task,groups,relations=None,revoke=None,review_change=None):
        plan={'events':groups,'relations':relations or [],'revoke':revoke or []}
        review={'events':[{'ref':g['ref'],'verdict':'supported','reason':'合成测试同一具体身份'} for g in groups],
          'facts':[{'ref':f['ref'],'verdict':'supported','reason':'合成测试逐事实原话','user_expression':True,
                    'recommended_operation':f['operation'],'target_fact':f.get('target_fact'),'adds_information':f['operation']!='duplicate'} for g in groups for f in g['facts']],
          'relations':[{'ref':r['ref'],'verdict':'supported','relationship_supported':True,'reason':'合成测试关系依据'} for r in relations or []],
          'revoke':[{'ref':r['ref'],'verdict':'supported','reason':'合成测试明确撤销'} for r in revoke or []]}
        if review_change: review_change(review)
        return d.commit(self.db,job,{'plan':plan,'review':review},{'model_calls':0,'synthetic':True},task['snapshot'])

    def create(self,text,identity='澄湾-A81'):
        job,task=self.prepare([text])
        result=self.commit(job,task,[self.group([self.fact(text)],identity)])
        return result['receipt']['memory_ids'][0]

    def test_multiround_event_and_each_fact_own_sources(self):
        first='E验收虚构：2022年澄湾-A81夜航前发现备用电池过期。'
        event=self.create(first)
        later='E验收虚构：澄湾-A81那次最后借到绿色电池，顺利完成夜航。'
        job,task=self.prepare([later]); old=next(m for m in task['context']['existing'] if m['event_key']=='澄湾-A81')
        self.commit(job,task,[self.group([self.fact(later,aspect='outcome',operation='supplement')],existing=old['id'])])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0],1)
        record=queries.search(self.db,{'query':'澄湾-A81'})['records'][0]
        self.assertEqual(record['fact_count'],2)
        self.assertEqual({f['aspect'] for f in record['facts']},{'process','outcome'})
        self.assertTrue(all(len(f['evidence'])==1 for f in record['facts']))
        evidence=queries.evidence(self.db,{'memory_id':event})
        self.assertEqual(len(evidence['records']),2)
        self.assertEqual(len({r['fact_id'] for r in evidence['records']}),2)

    def test_semantic_duplicate_adds_evidence_not_fact(self):
        event=self.create('E验收虚构：澄湾-A81那次借了绿色电池。')
        again='E验收虚构：澄湾-A81那晚的电池是借来的，颜色是绿的。'
        job,task=self.prepare([again]); old=task['context']['existing'][0]
        fact=self.fact(again,operation='duplicate',target_fact=old['facts'][0]['id'])
        receipt=self.commit(job,task,[self.group([fact],existing=old['id'])])['receipt']
        self.assertEqual(receipt['duplicates_avoided'],1)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM event_facts').fetchone()[0],1)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM fact_evidence').fetchone()[0],2)

    def test_meaning_change_keeps_history_and_explicit_correction(self):
        event=self.create('E验收虚构：澄湾-A81那次我觉得自己的准备很失败。')
        self.db.db.execute("UPDATE event_facts SET aspect='meaning'")
        later='E验收虚构：2025年再看澄湾-A81那次，我觉得它很有价值。'
        job,task=self.prepare([later]); old=task['context']['existing'][0]
        self.commit(job,task,[self.group([self.fact(later,aspect='meaning',time='2025',operation='meaning_change',target_fact=old['facts'][0]['id'])],existing=old['id'])])
        rows=self.db.db.execute('SELECT state,event_time FROM event_facts ORDER BY created_at').fetchall()
        self.assertEqual([r['state'] for r in rows],['historical','current'])
        self.assertEqual([r['event_time'] for r in rows],['2022','2025'])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0],2)

    def test_source_union_and_missing_review_never_authorize_inference(self):
        job,task=self.prepare(['E验收虚构：澄湾-A81起航。'])
        fact=self.fact('E验收虚构：澄湾-A81起航。',text='用户终身害怕承担责任',aspect='feeling')
        with self.assertRaises(ValueError):
            self.commit(job,task,[self.group([fact])],review_change=lambda r:r['facts'][0].update(verdict='rejected',user_expression=False))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0],0)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM compact_sources').fetchone()[0],0)

    def test_event_identity_cannot_merge_nearby_similar_event(self):
        self.create('E验收虚构：澄湾-A81借了绿色电池。')
        text='E验收虚构：2024年雾湾-B93也借了绿色电池，这是另一件事。'
        job,task=self.prepare([text]); old=task['context']['existing'][0]
        with self.assertRaises(ValueError):
            self.commit(job,task,[self.group([self.fact(text)],existing=old['id'])])

    def test_supported_relations_direction_staleness_and_revoke(self):
        a=self.create('E验收虚构：澄湾-A81那次我学会出发前检查电池。')
        b=self.create('E验收虚构：石岭-B63比赛前我也做了物品检查。','石岭-B63')
        text='E验收虚构：因为澄湾-A81那次经历，我在石岭-B63比赛也决定使用检查清单。'
        job,task=self.prepare([text]); bykey={m['event_key']:m for m in task['context']['existing']}
        aa,bb=bykey['澄湾-A81'],bykey['石岭-B63']
        relation={'ref':'r1','from':aa['id'],'to':bb['id'],'kind':'reported_influence','reason':'用户明确报告从夜航准备迁移到比赛准备',
          'evidence':[{'source':f['evidence'][0]['source'],'quote':f['evidence'][0]['quote']} for m in (aa,bb) for f in m['facts']]+[{'source':'u0','quote':text}]}
        self.commit(job,task,[],[relation])
        rel=dict(self.db.db.execute('SELECT * FROM event_relations').fetchone())
        self.assertEqual((rel['left_id'],rel['right_id'],rel['state']),(a,b,'supported'))
        paths=queries.explore(self.db,{'memory_ids':[b],'include_candidates':False})['records']
        linked=next(r for r in paths if r['id']==a)
        self.assertEqual(linked['path'][0]['traversal'],'reverse')
        self.assertEqual(linked['path'][0]['from'],a)
        self.assertTrue(queries.evidence(self.db,{'memory_id':rel['id']})['records'][0]['text'])
        denial='E验收虚构：澄湾-A81与石岭-B63无关，我否认此前的影响关系。'
        job,task=self.prepare([denial]); old=task['context']['relations'][0]
        self.commit(job,task,[],revoke=[{'ref':'x1','relation':old['id'],'reason':'用户否认原影响','evidence':[{'source':'u0','quote':denial}]}])
        self.assertEqual(self.db.db.execute('SELECT state FROM event_relations').fetchone()[0],'revoked')
        self.assertFalse(any(r['route']=='evidence_supported_relation' for r in queries.explore(self.db,{'memory_ids':[a]})['records']))

    def test_sqlite_failure_rolls_back_event_facts_and_d_receipt(self):
        self.db.db.execute("CREATE TRIGGER fail_e BEFORE INSERT ON compact_sources BEGIN SELECT RAISE(ABORT,'test'); END")
        job,task=self.prepare(['E验收虚构：澄湾-A81借了绿色电池。'])
        with self.assertRaises(Exception):
            self.commit(job,task,[self.group([self.fact(task['context']['messages'][0]['text'])])])
        for table in ('memory_events','event_facts','fact_evidence','summaries','compact_claims'):
            self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_review_must_correct_false_supplement_into_duplicate(self):
        self.create('E验收虚构：澄湾-A81借了绿色电池。')
        text='E验收虚构：澄湾-A81电池不是自带，是借来的绿色电池。'
        job,task=self.prepare([text]); old=task['context']['existing'][0]
        self.commit(job,task,[self.group([self.fact(text,operation='supplement')],existing=old['id'])],
          review_change=lambda r:r['facts'][0].update(recommended_operation='duplicate',target_fact=old['facts'][0]['id'],adds_information=False))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM event_facts').fetchone()[0],1)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM fact_evidence').fetchone()[0],2)

    def test_old_evidence_alone_cannot_create_new_fact(self):
        self.create('E验收虚构：澄湾-A81借了绿色电池。')
        text='E验收虚构：澄湾-A81那次我又补充了新的信息。'
        job,task=self.prepare([text]);old=task['context']['existing'][0]; h=old['facts'][0]['evidence'][0]
        group=self.group([self.fact(h['quote'],h['source'])],existing=old['id']);group['anchors']=[{'source':'u0','quote':text}]
        with self.assertRaisesRegex(ValueError,'new_human'):
            self.commit(job,task,[group])

    def test_candidate_cause_does_not_enter_formal_graph(self):
        a=self.create('E验收虚构：澄湾-A81借了绿色电池。')
        b=self.create('E验收虚构：石岭-B63借了蓝色电池。','石岭-B63')
        job,task=self.prepare(['我想了解这些记录。'])
        old={m['event_key']:m for m in task['context']['existing']}
        rel={'ref':'r1','from':old['澄湾-A81']['id'],'to':old['石岭-B63']['id'],'kind':'possible_cause',
             'reason':'合成候选，尚无影响证据','evidence':[m['facts'][0]['evidence'][0] for m in old.values()]}
        self.commit(job,task,[],[rel])
        self.assertEqual(self.db.db.execute('SELECT state FROM event_relations').fetchone()[0],'candidate')
        self.assertFalse(any(r['route']=='evidence_supported_relation' for r in queries.explore(self.db,{'memory_ids':[a],'include_candidates':False})['records']))

    def test_correction_invalidates_relation_review_of_cited_fact(self):
        a=self.create('E验收虚构：澄湾-A81借了绿色电池。')
        b=self.create('E验收虚构：石岭-B63借了绿色电池。','石岭-B63')
        job,task=self.prepare(['我继续核对两次借电池的记录。'])
        old={m['event_key']:m for m in task['context']['existing']}
        rel={'ref':'r1','from':old['澄湾-A81']['id'],'to':old['石岭-B63']['id'],'kind':'similar_context',
             'reason':'两次均临时借电池','evidence':[m['facts'][0]['evidence'][0] for m in old.values()]}
        self.commit(job,task,[],[rel])
        self.db.db.execute("UPDATE event_facts SET state='corrected' WHERE event_id=?",(a,))
        self.assertFalse(any(r['route']=='evidence_supported_relation' for r in queries.explore(self.db,{'memory_ids':[a],'include_candidates':False})['records']))

    def test_multiple_6000_character_segments_keep_exact_late_offsets(self):
        lead='E验收虚构：澄湾-A81出发前带了记录本。'
        tail='澄湾-A81回程后把记录本放到蓝色柜子的第二格。'
        text=lead+'填'*(6000-len(lead))+tail
        self.counter+=1
        src={'namespace':'n','session_id':'s','event_seq':99,'message_id':'long',
             'event_time':'2026-11-05T00:00:00Z','role':'user','source_kind':'user','text':text}
        manifest={'version':1,'authority':'host_compaction_opt_in/v1','namespace':'n','session_id':'s','compaction_id':'long',
          'target':{'provider':'p','model':'main'},'sources':[{'source':src,'start':0,'end':6000},{'source':src,'start':6000,'end':len(text)}]}
        job=d.enqueue(self.db,manifest)['job_id'];task=d.prepare(self.db,job)
        result=self.commit(job,task,[self.group([self.fact(lead),self.fact(tail,'u1',ref='a2',aspect='outcome')])])
        event=result['receipt']['memory_ids'][0]
        late=next(r for r in queries.evidence(self.db,{'memory_id':event})['records'] if r['text']==tail)
        self.assertEqual(late['text_offset'],6000)
        _,next_task=self.prepare(['E验收虚构：澄湾-A81那次我现在想核对记录本位置。'])
        context,env=events.context(self.db,[],self.db.recent_summaries_for_extraction('澄湾-A81'))
        old=context['existing'][0]['facts'][1]['evidence'][0]
        self.assertEqual(events.citations([old],env,require_new=False)[0]['start'],6000)

    def test_short_event_identity_beats_distractor_that_only_mentions_it(self):
        main=self.create('E验收虚构：2021年澄湾-A81夜航中借了电池。','2021年澄湾-A81夜航')
        self.create('E验收虚构：2024年雾湾-C92夜航也借了电池，但与澄湾-A81无关。','2024年雾湾-C92夜航')
        records=queries.search(self.db,{'query':'澄湾-A81那次','limit':2})['records']
        self.assertEqual(records[0]['id'],main)
        self.assertTrue(records[0]['signals']['exact_event_identity'])

    def test_failed_semantic_review_stops_automatic_calls_but_retains_range(self):
        job,task=self.prepare(['E验收虚构：澄湾-A81借了绿色电池。'])
        for index in range(3):
            if index: d.prepare(self.db,job)
            d.handle(self.db,{'action':'fail','job_id':job,'error':'semantic_validation_rejected'})
        row=dict(self.db.db.execute('SELECT * FROM compact_jobs WHERE id=?',(job,)).fetchone())
        self.assertEqual(row['state'],'needs_review')
        self.assertIn('澄湾-A81',row['payload'])
        self.assertFalse(d.handle(self.db,{'action':'pending'})['jobs'])
        result=d.handle(self.db,{'action':'retry','job_id':job})
        self.assertEqual(result['state'],'pending')
        self.assertEqual(d.handle(self.db,{'action':'pending'})['jobs'][0]['id'],job)

    def test_human_disclaimer_is_not_confused_with_an_actual_model_guess(self):
        text='E验收虚构：澄湾-A81那次我借了电池，这是我明确讲述的经历，不是模型推测。'
        event=self.create(text)
        self.assertEqual(queries.evidence(self.db,{'memory_id':event})['records'][0]['text'],text)
        job,task=self.prepare(['模型推测：澄湾-A81那次用户害怕失败。'])
        with self.assertRaisesRegex(ValueError,'not_user_asserted_fact'):
            self.commit(job,task,[self.group([self.fact(task['context']['messages'][0]['text'])],existing=task['context']['existing'][0]['id'])])

    def test_visible_candidate_review_can_select_existing_endpoint_facts(self):
        self.create('E验收虚构：澄湾-A81那次决定提前检查电池。')
        self.create('E验收虚构：石岭-B63比赛前也做了物品清点。','石岭-B63')
        text='E验收虚构：因为澄湾-A81那次经历，我在石岭-B63比赛采用了清单。'
        job,task=self.prepare([text]);old={m['event_key']:m for m in task['context']['existing']}
        relation={'ref':'new_r1','from':old['澄湾-A81']['id'],'to':old['石岭-B63']['id'],
            'kind':'reported_influence','reason':'用户报告影响，首轮故意缺少端点引用',
            'evidence':[{'source':'u0','quote':text}]}
        self.commit(job,task,[],[relation])
        self.assertEqual(self.db.db.execute('SELECT state FROM event_relations').fetchone()[0],'candidate')
        job,task=self.prepare([text]);old={m['event_key']:m for m in task['context']['existing']}
        rel=task['context']['relations'][0]
        def change(review):
            review['relations']=[{'ref':rel['id'],'verdict':'supported','reason':'逐端点事实核对与用户明确影响原话',
                'relationship_supported':True,'from_fact':old['澄湾-A81']['facts'][0]['id'],
                'to_fact':old['石岭-B63']['facts'][0]['id'],
                'relationship_evidence':[{'source':'u0','quote':text}]}]
        self.commit(job,task,[],review_change=change)
        self.assertEqual(self.db.db.execute('SELECT state FROM event_relations').fetchone()[0],'supported')
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM relation_history').fetchone()[0],2)

    def test_schema5_rollback_copy_preserves_new_event_aggregate_and_receipt(self):
        from scripts.rollback_e_clone import clone
        event=self.create('E验收虚构：澄湾-A81那次完成夜航。')
        target=Path(self.temp.name)/'rollback.sqlite3'
        clone(self.db.path,target)
        other=MemoryDB(target)
        try:
            self.assertEqual(other.db.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertIn('完成夜航',other.db.execute('SELECT text FROM summaries WHERE id=?',(event,)).fetchone()[0])
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM compact_jobs WHERE receipt IS NOT NULL').fetchone()[0],1)
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0],1)
        finally:other.close()


if __name__=='__main__': unittest.main()
