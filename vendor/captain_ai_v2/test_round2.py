import copy
import json
import unittest
import threading
import urllib.request
from unittest.mock import patch
from captain_ai import Client, normalize, PROTOCOL_VERSION, can_auto_store
from test_captain import VALID,REQUEST
from backend_contract import validate_response, storage_route, is_confirmed_classification,call_gateway
from server import make_server

SUMMARY={'total_errors':11,'mastered_errors':0,'practice':{'total':704,'correct':445},'accuracy':63.2,
 'period':{'from':'2026-09-15','to':'2026-09-21'},
 'subjects':[{'code':'POSTGRAD_MATH','name':'考研数学','error_count':4},{'code':'CET4','name':'英语四级','error_count':0}],
 'points':[{'name':'偏导数与高阶偏导数','subject_code':'POSTGRAD_MATH','error_count':11,'correct_count':None,
 'note':'正确题量暂无数据源（练习记录未记考点）'}],
 'confirmed_error_types':{},'pending_review':7,'available_minutes_per_day':None,'plan_days':None}

class Round2Tests(unittest.TestCase):
    def client(self,data=VALID):return Client(transport=lambda _:json.dumps(data))
    def test_protocol_metadata_success_and_failures(self):
        for req in [REQUEST,{},dict(REQUEST,scene='school_recommend')]:
            r=self.client().call(req)
            self.assertEqual(r['protocol']['version'],PROTOCOL_VERSION)
            self.assertEqual(r['protocol']['retry_owner'],'gateway')
            self.assertIsInstance(r['raw'],dict)
    def test_politics_object_answer_is_used(self):
        req=dict(REQUEST,scene='qa',subject='politics',input={'question':'实践与认识的关系','student_answer':'实践是认识的基础'},user_context={'task':'politics_quiz'})
        p,task=normalize(req)
        self.assertEqual(p['user_context']['student_answer'],'实践是认识的基础')
        data={'question':'实践与认识的关系','reference_answer':'参考答案','key_points':['实践'],'feedback':'仍需补充'}
        r=self.client(data).call(req)
        self.assertEqual(r['code'],0);self.assertEqual(r['data']['feedback'],'仍需补充')
    def test_conflicting_answer_rejected(self):
        req=dict(REQUEST,input={'question':'题目','student_answer':'A'},user_context={'student_answer':'B'})
        self.assertEqual(self.client().call(req)['code'],2)
    def test_object_unknown_field_rejected(self):
        self.assertEqual(self.client().call(dict(REQUEST,input={'question':'题目','bad':1}))['code'],2)
    def test_subject_code_normalized(self):
        self.assertEqual(normalize(dict(REQUEST,subject='POSTGRAD_MATH'))[0]['subject'],'math')
    def test_plan_clamp_and_null_defaults(self):
        for ctx,expected in [({'plan_days':99,'available_minutes_per_day':-1},(7,1)),
                             ({'plan_days':None,'available_minutes_per_day':None},(3,60)),
                             ({'plan_days':'2','available_minutes_per_day':'30'},(2,30))]:
            req=dict(REQUEST,scene='qa',user_context=dict(ctx,task='study_plan'));p,_=normalize(req)
            self.assertEqual((p['user_context']['plan_days'],p['user_context']['available_minutes_per_day']),expected)
    def test_plan_invalid_type_is_not_silently_defaulted(self):
        for value in [True,1.5,[],{},'abc']:
            req=dict(REQUEST,scene='qa',user_context={'task':'study_plan','plan_days':value})
            self.assertEqual(self.client().call(req)['code'],2)
    def test_actual_summary_no_fabricated_point_accuracy(self):
        req=dict(REQUEST,scene='weak_diagnosis',user_context={'summary':copy.deepcopy(SUMMARY)})
        with patch.object(Client,'_ollama',side_effect=AssertionError('must not infer')):r=Client().call(req)
        self.assertEqual(r['code'],0);self.assertEqual(r['data']['weak_points'],[])
        self.assertIn('63.2%',r['data']['summary']);self.assertIn('不能计算正确率',r['data']['summary'])
        self.assertIsNone(req['user_context']['summary']['points'][0]['correct_count'])
        self.assertEqual(r['model'],'deterministic-statistics-v2')
    def test_known_point_counts_and_subject_scope(self):
        summary={'knowledge_points':[{'name':'导数','total':10,'correct':3},{'name':'积分','total':10,'correct':9},
          {'name':'词汇','subject_code':'CET4','total':10,'correct':1}]}
        r=self.client().call(dict(REQUEST,scene='weak_diagnosis',user_context={'summary':summary}))
        self.assertEqual([p['name'] for p in r['data']['weak_points']],['导数'])
    def test_bad_counts_rejected(self):
        req=dict(REQUEST,scene='weak_diagnosis',user_context={'summary':{'practice':{'total':2,'correct':3}}})
        self.assertEqual(self.client().call(req)['code'],2)
    def test_missing_answer_confidence_forced_zero(self):
        for answer in ['',None,'（OCR 未识别出手写作答）']:
            r=self.client().call(dict(REQUEST,input={'question':'2x+3=7','student_answer':answer},user_context={}))
            self.assertEqual(r['data']['error_confidence'],0);self.assertEqual(r['raw']['model_error_confidence'],0.8)
            self.assertFalse(can_auto_store(r,quality_gate_passed=True));self.assertEqual(storage_route(r),'manual_review_queue')
    def test_pending_legacy_label_excluded(self):
        self.assertFalse(is_confirmed_classification({'error_type':'calculation_error','review_flag':'pending_review'}))
        self.assertTrue(is_confirmed_classification({'error_type':'calculation_error','review_flag':None}))
    def test_response_validation_does_not_relabel_protocol(self):
        r=self.client().call(REQUEST);self.assertIs(validate_response(REQUEST,r),r)
        r['protocol']['version']='old'
        with self.assertRaises(ValueError):validate_response(REQUEST,r)
    def test_invalid_response_schema_is_rejected(self):
        r=self.client().call(REQUEST);del r['data']['solution']
        with self.assertRaises(ValueError):validate_response(REQUEST,r)
    def test_school_and_judge_do_not_call_model(self):
        with patch.object(Client,'_ollama',side_effect=AssertionError('must not infer')):
            self.assertEqual(Client().call(dict(REQUEST,scene='school_recommend'))['code'],1)
            self.assertEqual(Client().call(dict(REQUEST,scene='judge_fragment'))['code'],2)
    def test_real_http_stub_boundary_one_request_for_code2(self):
        calls=[];client=self.client();original=client.call
        def track(req):calls.append(req);return original(req)
        client.call=track
        server=make_server(0,client);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            r=call_gateway({},'http://127.0.0.1:%d/ai'%server.server_port)
            self.assertEqual(r['code'],2);self.assertEqual(len(calls),1)
        finally:server.shutdown();server.server_close();thread.join()
    def test_live_schema_endpoint_matches_file_contract(self):
        server=make_server(0,self.client());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/schema'%server.server_port) as response:
                schema=json.load(response)
            self.assertEqual(schema['protocol']['version'],PROTOCOL_VERSION)
            self.assertIn('politics_quiz',schema['schemas'])
            self.assertIn('reference_answer',schema['schemas']['politics_quiz']['required'])
        finally:server.shutdown();server.server_close();thread.join()
    def test_raw_string_and_boolean_code_rejected(self):
        for changes in [{'raw':'plain text'},{'code':True}]:
            r=self.client().call(REQUEST);r.update(changes)
            with self.assertRaises(ValueError):validate_response(REQUEST,r)
    def test_foreign_subject_points_not_diagnosed(self):
        req=dict(REQUEST,scene='weak_diagnosis',user_context={'summary':{'points':[
           {'name':'四级词汇','subject_code':'CET4','total':10,'correct':0}]}})
        self.assertEqual(self.client().call(req)['data']['weak_points'],[])

if __name__=='__main__':unittest.main(verbosity=2)
