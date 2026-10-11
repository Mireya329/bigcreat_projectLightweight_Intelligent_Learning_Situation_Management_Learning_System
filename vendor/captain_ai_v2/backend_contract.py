"""Gateway boundary for member 2; never replaces their ai_interface.py."""
import json
import os
import urllib.request
import urllib.error
from captain_ai import PROTOCOL_VERSION, SCHEMA_VERSION, SCHEMAS, envelope, normalize, validate, strict_loads, LABELS

def validate_response(request, response):
    if type(response) is not dict:raise ValueError('response must be an object')
    if not {'code','scene','data','raw','model','elapsed_ms','protocol'}<=set(response):raise ValueError('missing envelope fields')
    if type(response['code']) is not int or response['code'] not in (0,1,2):raise ValueError('invalid code')
    if type(response['raw']) is not dict:raise ValueError('raw must be an object')
    if type(response['model']) is not str:raise ValueError('model must be text')
    if type(response['elapsed_ms']) is not int or response['elapsed_ms']<0:raise ValueError('invalid elapsed_ms')
    protocol=response['protocol']
    if type(protocol) is not dict or protocol.get('version')!=PROTOCOL_VERSION or protocol.get('schema_version')!=SCHEMA_VERSION or protocol.get('retry_owner')!='gateway':
        raise ValueError('protocol mismatch; do not silently rewrite metadata')
    if response['code']!=0:
        if response['data'] is not None:raise ValueError('failure data must be null')
        return response
    req,task=normalize(request)
    if response['scene']!=req['scene']:raise ValueError('scene mismatch')
    if task not in SCHEMAS:raise ValueError('success for unavailable scene')
    data=response['data'];validate(data,SCHEMAS[task])
    if task=='error_analysis' and data['error_type_label']!=LABELS[data['error_type']]:raise ValueError('label mismatch')
    if task=='study_plan':
        ctx=req['user_context']
        if sorted(p['day'] for p in data['plan'])!=list(range(1,ctx['plan_days']+1)):raise ValueError('invalid plan days')
        if any(p['minutes']>ctx['available_minutes_per_day'] for p in data['plan']):raise ValueError('plan over budget')
    return response

def call_gateway(request, url=None, timeout=None):
    """Exactly one HTTP attempt. Failures are envelopes, never model prose."""
    url=url or os.environ.get('AI_GATEWAY_URL','http://127.0.0.1:8765/ai')
    timeout=float(timeout or os.environ.get('AI_GATEWAY_TIMEOUT','370'))
    try:payload=json.dumps(request,ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (TypeError,ValueError) as exc:
        return envelope(2,raw={'text':'','reason':'invalid_request: '+str(exc),'retried':0})
    req=urllib.request.Request(url,data=payload,headers={'Content-Type':'application/json'},method='POST')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req,timeout=timeout) as response:text=response.read(2_000_000).decode('utf-8')
        return validate_response(request,strict_loads(text))
    except urllib.error.HTTPError as exc:
        return envelope(1,raw={'text':exc.read(65536).decode('utf-8','replace'),
            'reason':'HTTP failure; correct request or retry later as appropriate','http_status':exc.code,'retried':0})
    except (OSError,ValueError,TypeError,KeyError) as exc:
        return envelope(1,raw={'text':locals().get('text',''),'reason':'gateway_or_contract_error: '+str(exc),'retried':0})

def storage_route(response):
    if response.get('code')!=0:return 'failure_log_only'
    if response.get('scene')=='error_analysis':return 'manual_review_queue'
    return 'validated_result'

def is_confirmed_classification(row):
    return row.get('review_flag') in (None,'') and row.get('error_type') in LABELS
