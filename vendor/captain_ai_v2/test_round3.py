import json
import unittest
from captain_ai import Client,LABELS
from test_captain import VALID,REQUEST

class Round3Tests(unittest.TestCase):
    def call(self,data):
        self.calls=[]
        def transport(p):self.calls.append(p);return json.dumps(data,ensure_ascii=False)
        return Client(transport=transport).call(REQUEST)
    def test_explicit_conclusion_repairs_empty_answer(self):
        data=dict(VALID,answer='',solution=['2x=4','最终答案：x=2'])
        r=self.call(data)
        self.assertEqual(r['code'],0);self.assertEqual(r['data']['answer'],'x=2')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(json.loads(r['raw']['text'])['answer'],'')
        self.assertEqual(r['raw']['normalizations'][0]['field'],'answer')
    def test_arbitrary_last_step_is_not_promoted(self):
        for text in ['接下来代入公式','2x=4','答案：未知','答案：可能为2','假设答案：2','答案：？']:
            with self.subTest(text=text):
                r=self.call(dict(VALID,answer='',solution=[text]))
                self.assertEqual(r['code'],1);self.assertEqual(len(self.calls),2)
    def test_missing_or_wrong_type_answer_is_not_fabricated(self):
        for value in [None,False,[]]:
            self.assertEqual(self.call(dict(VALID,answer=value,solution=['答案：x=2']))['code'],1)
        missing=dict(VALID,solution=['答案：x=2']);del missing['answer']
        self.assertEqual(self.call(missing)['code'],1)
    def test_other_invalid_fields_still_fail(self):
        self.assertEqual(self.call(dict(VALID,answer='',solution=['答案：x=2'],error_type='unknown'))['code'],1)
    def test_label_is_derived_from_valid_code(self):
        r=self.call(dict(VALID,error_type_label='概念理解错误'))
        self.assertEqual(r['code'],0)
        self.assertEqual(r['data']['error_type_label'],LABELS[VALID['error_type']])
        self.assertEqual(len(self.calls),1)
    def test_nonempty_answer_never_overwritten(self):
        r=self.call(dict(VALID,answer='x=3',solution=['答案：x=2']))
        self.assertEqual(r['data']['answer'],'x=3')
        self.assertNotIn('normalizations',r['raw'])

if __name__=='__main__':unittest.main(verbosity=2)
