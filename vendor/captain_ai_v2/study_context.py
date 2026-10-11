"""Evidence-based statistics; missing counts are never converted to zero."""
import math

SUBJECT_MAP = {'POSTGRAD_MATH':'math', 'POSTGRAD_POLITICS':'politics',
               'POSTGRAD_ENGLISH':'english', 'CET4':'cet4', 'CET6':'cet6'}

def count(value):
    return type(value) is int and value >= 0

def diagnose(summary, subject):
    if type(summary) is not dict or not summary:
        raise ValueError('summary must be a nonempty object')
    notes=[]
    weak=[]
    practice=summary.get('practice')
    if practice is not None:
        if type(practice) is not dict:raise ValueError('summary.practice must be an object')
        total,correct=practice.get('total'),practice.get('correct')
    else:
        total,correct=summary.get('total_questions'),summary.get('correct_questions')
    if total is not None and not count(total):raise ValueError('invalid practice total')
    if correct is not None and not count(correct):raise ValueError('invalid practice correct count')
    if total is not None and correct is not None:
        if correct>total:raise ValueError('correct count exceeds total')
        if total:
            notes.append('所提供统计范围内共%d次作答，正确%d次，正确率%.1f%%；不能将整体正确率当成单个考点正确率。'%(total,correct,100*correct/total))
        else:notes.append('统计范围内暂无作答记录。')
    rows=summary.get('knowledge_points',summary.get('points',[]))
    if type(rows) is not list:raise ValueError('summary points must be an array')
    unknown=[]
    for row in rows:
        if type(row) is not dict:raise ValueError('point must be an object')
        name=row.get('name')
        if type(name) is not str or not name.strip():raise ValueError('point name required')
        code=row.get('subject_code',row.get('subject'))
        if code is not None and SUBJECT_MAP.get(code,code)!=subject:continue
        total=row.get('total',row.get('total_count'))
        correct=row.get('correct',row.get('correct_count'))
        errors=row.get('error_count')
        for key,value in [('total',total),('correct',correct),('error_count',errors)]:
            if value is not None and not count(value):raise ValueError(name+': invalid '+key)
        if total is not None and correct is not None:
            if correct>total:raise ValueError(name+': correct exceeds total')
            if total<5:
                unknown.append(name+'仅%d次作答，样本不足，暂不认定薄弱。'%total)
            elif correct/total<0.6:
                weak.append({'name':name,'evidence':'%d次作答中正确%d次，正确率%.1f%%。'%(total,correct,100*correct/total),
                             'advice':'优先复习该考点，完成5道有参考答案的练习并记录作答结果。'})
        else:
            evidence='，现有%d条错题标签记录'%errors if errors is not None else ''
            unknown.append(name+'缺少完整作答分母或正确题量'+evidence+'；不能计算正确率，也不能认定正确率为0%。')
    notes.extend(unknown)
    if len(weak)>8:
        notes.append('薄弱点超过8项，当前展示前8项。')
        weak=weak[:8]
    pending=summary.get('pending_review')
    if pending is not None:
        if not count(pending):raise ValueError('invalid pending_review')
        notes.append('%d条待复核归因不纳入已确认错误类型结论。'%pending)
    if not notes:notes.append('当前数据不足以给出可靠的考点正确率诊断。')
    if weak:notes.append('有完整作答数据支持的优先复习考点：'+ '、'.join(p['name'] for p in weak)+'。')
    suggestions=['补录练习与考点的关联后再计算考点正确率。' if unknown else '继续记录按考点区分的真实作答结果。',
                 '先人工核对待复核归因，原始答题事实与AI分类分开统计。']
    return {'summary':' '.join(notes),'weak_points':weak,'suggestions':suggestions}
