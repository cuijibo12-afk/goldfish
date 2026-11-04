import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from goldfish.engine import MemoryDB
from goldfish.providers import ChatModel, EmbeddingModel, ModelConfig, _check_url, load_config
from goldfish.server import Service

class ProviderTests(unittest.TestCase):
    def test_ollama_json_chat(self):
        config=ModelConfig('LLM','ollama','http://127.0.0.1:11434','qwen3:4b')
        client=ChatModel(config)
        def fake_post(url, payload, key, timeout=60):
            self.assertEqual(url,'http://127.0.0.1:11434/api/chat')
            self.assertEqual(payload['format'],'json')
            self.assertEqual(payload['stream'],False)
            self.assertEqual(payload['model'],'qwen3:4b')
            return {'message':{'content':'{"summaries":[],"links":[],"profiles":[]}'}}
        with patch('goldfish.providers._post_json',fake_post):
            self.assertEqual(client.extract([],[])['summaries'],[])

    def test_cloud_compatible_chat(self):
        config=ModelConfig('LLM','openai','https://api.example.com/v1','remote-model','test-key')
        def fake_post(url,payload,key,timeout=60):
            self.assertEqual(url,'https://api.example.com/v1/chat/completions')
            self.assertEqual(key,'test-key')
            self.assertEqual(payload['model'],'remote-model')
            return {'choices':[{'message':{'content':'```json\n{"summaries":[],"links":[],"profiles":[]}\n```'}}]}
        with patch('goldfish.providers._post_json',fake_post):
            self.assertEqual(ChatModel(config).extract([],[])['profiles'],[])

    def test_embeds_ollama_and_openai_shapes(self):
        local=EmbeddingModel(ModelConfig('EMBED','ollama','http://127.0.0.1:11434','bge-m3'))
        remote=EmbeddingModel(ModelConfig('EMBED','openai','https://embedding.example/v1','embed-001'))
        with patch('goldfish.providers._post_json',return_value={'embeddings':[[3,4],[0,5]]}) as mocked:
            vectors=local.embed(['中文记忆','另一条'])
            self.assertEqual(mocked.call_args.args[0],'http://127.0.0.1:11434/api/embed')
            self.assertAlmostEqual(vectors[0][0],.6)
        with patch('goldfish.providers._post_json',return_value={'data':[{'index':1,'embedding':[0,2]}, {'index':0,'embedding':[2,0]}]}) as mocked:
            vectors=remote.embed(['a','b'])
            self.assertEqual(mocked.call_args.args[0],'https://embedding.example/v1/embeddings')
            self.assertEqual(vectors,[[1.0,0.0],[0.0,1.0]])

    def test_local_and_cloud_can_mix_and_cloud_does_not_auto_activate(self):
        env={'GOLDFISH_LLM_PROVIDER':'openai','GOLDFISH_LLM_MODEL':'cloud-chat',
             'GOLDFISH_EMBED_PROVIDER':'ollama','GOLDFISH_EMBED_MODEL':'local-embed'}
        with patch.dict(os.environ,env,clear=True):
            self.assertFalse(load_config('LLM').configured)  # cloud URL must be explicit
            local=load_config('EMBED')
            self.assertTrue(local.configured)
            self.assertIn('127.0.0.1',local.endpoint)
            self.assertNotIn('key',str(local.public()).lower())

    def test_reject_unencrypted_remote(self):
        with self.assertRaises(ValueError): _check_url('http://some-public-domain.example/v1')
        _check_url('http://127.0.0.1:11434/api/chat')

    def test_semantic_recall_reaches_lexically_hidden_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=MemoryDB(Path(tmp)/'memory.db')
            m=db.add_message('s','user','我在陌生场合会焦虑')
            sid=db.add_summary('对未知的情况有担忧',[m])
            identity='ollama:http://localhost:11434:bge-m3'
            self.assertEqual(len(db.missing_embeddings(identity)),1)
            db.store_embeddings(identity,[(sid,[1,0])])
            self.assertEqual(len(db.missing_embeddings(identity)),0)
            scores=db.semantic_scores([1,0],identity)
            r=db.recall('害怕新的挑战',semantic_scores=scores,max_chars=2000)
            self.assertIn(sid,{x['id'] for x in r['results']})
            self.assertTrue('semantic' in r['mode'])
            # Switching models automatically marks old entries as needing vectors.
            self.assertEqual(len(db.missing_embeddings('new-model')),1)
            db.close()

    def test_service_wont_need_internet_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{},clear=True):
            service=Service(Path(tmp)/'memory.db')
            try:
                self.assertEqual(service.handle('/providers',{})['chat']['configured'],False)
                self.assertEqual(service.handle('/reindex',{})['reason'],'embedding_not_configured')
            finally:
                service.pool.shutdown(wait=True); service.db.close()

if __name__=='__main__': unittest.main()
