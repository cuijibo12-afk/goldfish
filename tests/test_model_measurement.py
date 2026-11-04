"""Protocol regressions for options and actual provider usage accounting."""
import os
import unittest
from unittest.mock import patch
from goldfish.providers import ChatModel, ModelConfig, load_config


class ModelMeasurementTests(unittest.TestCase):
    def test_deepseek_options_and_provider_usage_are_retained_without_content(self):
        config = ModelConfig('LLM', 'openai', 'https://api.deepseek.com', 'deepseek-flash',
                             'private-test-key', 'disabled', 8192)
        model = ChatModel(config)
        response = {'id': 'real-shape-id', 'model': 'deepseek-flash',
                    'usage': {'prompt_tokens': 41, 'completion_tokens': 17, 'total_tokens': 58},
                    'choices': [{'message': {'content': '{"summaries":[],"links":[],"profiles":[]}'}}]}
        with patch('goldfish.providers._post_json', return_value=response) as post:
            model.extract([{'text': 'private dialogue'}], [])
        payload = post.call_args.args[1]
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['max_tokens'], 8192)
        self.assertEqual(model.last_call['usage']['total_tokens'], 58)
        self.assertNotIn('private', str(model.last_call))
        self.assertEqual(model.last_call['response_id'], 'real-shape-id')

    def test_ollama_thinking_and_usage_remain_independent(self):
        model = ChatModel(ModelConfig('LLM', 'ollama', 'http://127.0.0.1:11434', 'qwen3', '', 'disabled', 2048))
        response = {'model': 'qwen3', 'prompt_eval_count': 40, 'eval_count': 15,
                    'message': {'content': '{"summaries":[],"links":[],"profiles":[]}'}}
        with patch('goldfish.providers._post_json', return_value=response) as post:
            model.extract([], [])
        self.assertIs(post.call_args.args[1]['think'], False)
        self.assertEqual(post.call_args.args[1]['options']['num_predict'], 2048)
        self.assertEqual(model.last_call['usage']['total_tokens'], 55)

    def test_failed_request_never_reuses_previous_usage(self):
        model = ChatModel(ModelConfig('LLM', 'openai', 'https://api.deepseek.com', 'deepseek-flash'))
        with patch('goldfish.providers._post_json', side_effect=TimeoutError('private response')):
            with self.assertRaises(TimeoutError): model.extract([], [])
        self.assertIsNone(model.last_call['usage'])
        self.assertEqual(model.last_call['error_type'], 'TimeoutError')
        self.assertNotIn('private', str(model.last_call))

    def test_model_options_are_explicit_and_validated(self):
        with patch.dict(os.environ, {'GOLDFISH_LLM_PROVIDER': 'ollama', 'GOLDFISH_LLM_MODEL': 'local',
            'GOLDFISH_LLM_THINKING': 'disabled', 'GOLDFISH_LLM_MAX_TOKENS': '4096'}, clear=True):
            self.assertEqual(load_config('LLM').thinking, 'disabled')
            self.assertEqual(load_config('LLM').max_tokens, 4096)
            self.assertEqual(load_config('EMBED').thinking, '')
        with patch.dict(os.environ, {'GOLDFISH_LLM_THINKING': 'invalid'}, clear=True):
            with self.assertRaises(ValueError): load_config('LLM')


if __name__ == '__main__': unittest.main()
