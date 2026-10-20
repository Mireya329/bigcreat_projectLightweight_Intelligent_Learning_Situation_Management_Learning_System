import copy
import json
import threading
import unittest
import urllib.request
from captain_ai import Client, SCHEMAS, validate, output_shape
from server import make_server
from test_captain import VALID, REQUEST

class Round4Tests(unittest.TestCase):
    def run_values(self, values):
        calls=[]
        it=iter(values)
        def transport(payload):
            calls.append(payload)
            return json.dumps(next(it),ensure_ascii=False)
        return Client(transport=transport).call(REQUEST),calls

    def test_placeholder_fails_after_one_retry(self):
        bad=dict(VALID,answer='请填写分析结果')
        result,calls=self.run_values([bad,bad])
        self.assertEqual(result['code'],1);self.assertIsNone(result['data'])
        self.assertEqual(len(calls),2);self.assertEqual(len(result['raw']['attempts']),2)
        self.assertIn('placeholder_text',result['raw']['reason'])
        self.assertEqual(json.loads(result['raw']['text'])['answer'],bad['answer'])

    def test_retry_can_recover(self):
        r,calls=self.run_values([dict(VALID,answer='请填写分析结果'),VALID])
        self.assertEqual(r['code'],0);self.assertEqual(r['data']['answer'],VALID['answer'])
        self.assertEqual(len(calls),2);self.assertEqual(r['raw']['retried'],1)

    def test_nested_free_text_and_whitespace_rejected(self):
        for field,value in [('answer',' \n请填写分析结果\t'),('solution',['请填写分析结果']),
                            ('knowledge_points',['请填写分析结果']),('error_reason','请填写分析结果'),
                            ('suggestions',['请填写分析结果'])]:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError,'placeholder_text'):
                    validate(dict(VALID,**{field:value}),SCHEMAS['error_analysis'])

    def test_all_schema_free_text_rejects_placeholder(self):
        def visit(schema):
            if schema['type']=='string' and 'enum' not in schema:
                with self.assertRaisesRegex(ValueError,'placeholder_text'):
                    validate(output_shape(schema),schema)
            elif schema['type']=='object':
                for child in schema['properties'].values():visit(child)
            elif schema['type']=='array':visit(schema['items'])
        for schema in SCHEMAS.values():visit(schema)

    def test_valid_enum_and_real_answers_preserved(self):
        for answer in ['x=2','0','文本中出现“请填写分析结果”是占位说明。']:
            validate(dict(VALID,answer=answer),SCHEMAS['error_analysis'])
        validate(VALID,SCHEMAS['error_analysis'])

    def test_empty_answer_repair_cannot_promote_placeholder(self):
        bad=dict(VALID,answer='',solution=['最终答案：请填写分析结果'])
        r,_=self.run_values([bad,bad]);self.assertEqual(r['code'],1)

    def test_empty_answer_explicit_conclusion_still_recovers(self):
        r,c=self.run_values([dict(VALID,answer='',solution=['最终答案：x=2'])])
        self.assertEqual(r['code'],0);self.assertEqual(r['data']['answer'],'x=2')
        self.assertEqual(len(c),1)

    def test_http_response_has_no_usable_placeholder_data(self):
        bad=dict(VALID,answer='请填写分析结果')
        server=make_server(0,Client(transport=lambda _:json.dumps(bad,ensure_ascii=False)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/ai',
                data=json.dumps(REQUEST).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as res:r=json.load(res)
            self.assertEqual(r['code'],1);self.assertIsNone(r['data'])
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
