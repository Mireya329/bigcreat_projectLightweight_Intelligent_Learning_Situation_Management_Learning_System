import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from captain_ai import Client, SCHEMAS, LABELS, can_auto_store, need_review, render_content, validate
from server import make_server

VALID = {'answer': 'x=2', 'solution': ['2x=4', 'x=2'], 'knowledge_points': ['移项'],
    'error_type': 'concept_misunderstanding', 'error_type_label': '概念不清',
    'error_confidence': 0.8, 'error_reason': '移项没有变号', 'suggestions': ['复习等式性质']}
REQUEST = {'scene': 'error_analysis', 'subject': 'math', 'input': '2x+3=7，学生解得x=5', 'user_context': {'student_answer':'2x=7+3，x=5'}}

class ProtocolTests(unittest.TestCase):
    def call(self, values, request=None):
        self.payloads = []
        iterator = iter(values)
        def fake(payload):
            self.payloads.append(payload)
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, str) else json.dumps(value)
        return Client(transport=fake).call(request or REQUEST)
    def test_valid_envelope_and_json_mode(self):
        r = self.call([VALID])
        self.assertEqual(set(r), {'code','scene','data','raw','model','elapsed_ms','protocol'})
        self.assertEqual(r['code'], 0)
        self.assertEqual(self.payloads[0]['format'], 'json')
        self.assertFalse(self.payloads[0]['stream'])
        self.assertFalse(can_auto_store(r))
        self.assertTrue(can_auto_store(r, quality_gate_passed=True))
    def test_one_retry_recovers(self):
        r = self.call(['not json', VALID])
        self.assertEqual(r['code'], 0)
        self.assertEqual(r['raw']['retried'], 1)
        self.assertEqual(len(self.payloads), 2)
    def test_two_failures_keep_raw_no_content(self):
        r = self.call(['first bad', 'second bad'])
        self.assertEqual(r['code'], 1)
        self.assertIsNone(r['data'])
        self.assertEqual(r['raw']['text'], 'second bad')
        self.assertEqual(r['raw']['attempts'][0]['text'], 'first bad')
        self.assertEqual(render_content(r), '')
        self.assertFalse(can_auto_store(r))
    def test_schema_rejections(self):
        for field, value in [('error_type','other'), ('error_confidence',True),
                             ('error_confidence',1.1), ('error_confidence',float('nan')),
                             ('error_type_label','错误'), ('solution',[]), ('answer',' ')]:
            with self.subTest(field=field, value=value):
                bad = dict(VALID, **{field:value})
                self.assertEqual(self.call([bad,bad])['code'],1)
    def test_missing_field(self):
        bad = dict(VALID)
        del bad['error_reason']
        self.assertIn('missing error_reason', self.call([bad,bad])['raw']['reason'])
    def test_confidence_boundary(self):
        for conf, expected in [(0,True), (0.49,True), (0.5,False), (1,False)]:
            r = self.call([dict(VALID,error_confidence=conf)])
            self.assertEqual(need_review(r), expected)
            self.assertEqual(can_auto_store(r, quality_gate_passed=True), not expected)
    def test_model_offline(self):
        r = self.call([ConnectionError('offline')])
        self.assertEqual(r['code'],1)
        self.assertEqual(len(self.payloads),1)
    def test_timeout_does_not_retry(self):
        r = self.call([TimeoutError('deadline')])
        self.assertEqual(r['code'],1)
        self.assertEqual(len(self.payloads),1)
    def test_error_after_invalid_output_keeps_evidence(self):
        r = self.call(['invalid',ConnectionError('offline')])
        self.assertEqual(r['raw']['text'],'invalid')
        self.assertEqual(r['raw']['retried'],1)
    def test_request_validation_no_model_call(self):
        for changes in [{'scene':[]},{'scene':'judge_fragment'},{'subject':'biology'},
                        {'input':''},{'user_context':[]},{'input':'a'*6001}]:
            self.assertEqual(self.call([],dict(REQUEST,**changes))['code'],2)
            self.assertEqual(self.payloads,[])
    def test_alias(self):
        self.assertEqual(self.call([VALID],dict(REQUEST,scene='explain_wrong'))['scene'],'error_analysis')
    def test_missing_school_data(self):
        self.assertEqual(self.call([],dict(REQUEST,scene='school_recommend'))['code'],1)
        self.assertEqual(self.payloads,[])
    def test_missing_stats(self):
        self.assertEqual(self.call([],dict(REQUEST,scene='weak_diagnosis'))['code'],2)
    def test_duplicate_json(self):
        bad = '{"answer":"a","answer":"b"}'
        self.assertEqual(self.call([bad,bad])['code'],1)
    def test_plan_budget_and_days(self):
        req = dict(REQUEST, scene='qa',user_context={'task':'study_plan','plan_days':1,'available_minutes_per_day':20})
        for day, minutes in [(1,21),(2,10)]:
            bad = {'summary':'练习','plan':[{'day':day,'task':'导数','minutes':minutes,'success_check':'做对3题'}]}
            self.assertEqual(self.call([bad,bad],req)['code'],1)
    def test_essay_correction_must_exist(self):
        req = dict(REQUEST,scene='essay_review',subject='english')
        bad = {'overall_feedback':'x','corrections':[{'original':'invented','corrected':'x','reason':'x'}],
               'revised_essay':'x','suggestions':['x']}
        self.assertEqual(self.call([bad,bad],req)['code'],1)
    def test_cpu_option(self):
        calls = []
        def fake(payload):
            calls.append(payload)
            return json.dumps(VALID)
        Client(transport=fake,cpu_only=True).call(REQUEST)
        self.assertEqual(calls[0]['options']['num_gpu'],0)
    def test_no_quiz_feedback_before_answer(self):
        req = dict(REQUEST,scene='qa',subject='politics',user_context={'task':'politics_quiz'})
        data = {'question':'问题','reference_answer':'参考答案','key_points':['要点'],'feedback':'回答完整'}
        r = self.call([data],req)
        self.assertEqual(r['data']['feedback'],'尚未作答')
        self.assertEqual(json.loads(r['raw']['text'])['feedback'],'回答完整')
    def test_plan_requires_measurable_check(self):
        req = dict(REQUEST,scene='qa',user_context={'task':'study_plan','plan_days':1})
        bad = {'summary':'复习','plan':[{'day':1,'task':'练习','minutes':10,'success_check':'掌握知识'}]}
        self.assertEqual(self.call([bad,bad],req)['code'],1)

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0,Client(transport=lambda p:json.dumps(VALID)))
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%s' % cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
    def test_http_round_trip(self):
        req = urllib.request.Request(self.url+'/ai',data=json.dumps(REQUEST).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(json.load(response)['code'],0)
    def test_health(self):
        with urllib.request.urlopen(self.url+'/health') as response:
            self.assertEqual(json.load(response)['status'],'gateway_running')
    def test_malformed_json(self):
        req = urllib.request.Request(self.url+'/ai',data=b'bad',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code,400)

if __name__ == '__main__':
    unittest.main(verbosity=2)
