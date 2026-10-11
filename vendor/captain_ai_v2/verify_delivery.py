"""Real HTTP acceptance: starts a temporary loopback gateway, or uses --url."""
import argparse
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from backend_contract import call_gateway, validate_response
from captain_ai import PROTOCOL_VERSION, SCHEMA_VERSION
from server import make_server

def main():
    p=argparse.ArgumentParser();p.add_argument('--url');args=p.parse_args()
    root=Path(__file__).resolve().parent
    server=None;thread=None
    if not args.url:
        server=make_server(0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        args.url='http://127.0.0.1:%d/ai'%server.server_port
    report={'started_at':datetime.now().astimezone().isoformat(),'protocol':PROTOCOL_VERSION,'schema':SCHEMA_VERSION,
            'transport':'HTTP -> gateway -> Ollama or deterministic statistics',
            'code_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in root.glob('*.py')},'cases':[]}
    folder=root/'reports';folder.mkdir(exist_ok=True)
    dest=folder/('acceptance-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    try:
        for case in json.loads((root/'examples.json').read_text(encoding='utf-8')):
            print('Running '+case['name'],flush=True)
            result=call_gateway(case['request'],args.url)
            passed=result['code']==case.get('expected_code',0)
            checks=[]
            if result['code']==0:
                try:
                    validate_response(case['request'],result)
                    if case['name']=='缺少学生作答':
                        assert result['data']['error_confidence']==0
                        checks.append('missing answer forced to zero confidence')
                    if case['name']=='实际学情缺少考点分母':
                        assert not result['data']['weak_points']
                        assert '不能计算正确率' in result['data']['summary']
                        checks.append('no fabricated point accuracy')
                except (AssertionError,ValueError) as exc:passed=False;checks.append(str(exc))
            report['cases'].append(dict(case,response=result,passed_contract=passed,checks=checks))
            dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('code=%s passed=%s elapsed_ms=%s'%(result['code'],passed,result['elapsed_ms']),flush=True)
    finally:
        if server:server.shutdown();server.server_close();thread.join()
    report['finished_at']=datetime.now().astimezone().isoformat()
    report['passed']=sum(c['passed_contract'] for c in report['cases']);report['total']=len(report['cases'])
    report['note']='Contract checks, not comprehensive content-quality or original backend acceptance.'
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(dest,flush=True)
    return 0 if report['passed']==report['total'] else 1

if __name__=='__main__':raise SystemExit(main())
