"""Deterministic spreadsheet operations over authorized source rows, never model-written code."""
from decimal import Decimal, InvalidOperation
from . import files

def run(host,user,args):
    action=args['action'];ids=args['file_ids']
    if action not in {'summarize','duplicates','reconcile'} or not isinstance(ids,list) or not 1<=len(ids)<=2:raise ValueError()
    indices=args.get('sheet_indices',[0,0])
    if not isinstance(indices,list) or len(indices)<len(ids):raise ValueError()
    sources=[]
    for index,fid in enumerate(ids):
        content=files.extract(host,user,fid)
        sheet_index=int(indices[index])
        tables=content['tables']
        if not 0<=sheet_index<len(tables):raise ValueError('sheet_not_found')
        table=tables[sheet_index];rows=table['rows'];start=int(args.get('header_rows',1))
        if not 0<=start<=10:raise ValueError()
        sources.append((fid,table['sheet'],[(i+1,r) for i,r in enumerate(rows) if i>=start]))
    key_col=int(args.get('key_column',0));amount_col=int(args.get('amount_column',1))
    if not 0<=key_col<80 or not 0<=amount_col<80:raise ValueError()
    def key(row):return str(row[key_col]).strip() if len(row)>key_col else ''
    def money(row):
        if len(row)<=amount_col or str(row[amount_col]).strip()=='':return None
        try:
            raw=str(row[amount_col]).strip().replace(',','').replace('￥','').replace('¥','')
            value=Decimal(raw)
            return value if value.is_finite() else None
        except InvalidOperation:return None
    def evidence(source,number,row):return {'file_id':source[0],'sheet':source[1],'row':number,'original':row}
    if action=='summarize':
        out=[]
        for source in sources:
            values=[money(r) for _,r in source[2]];known=[v for v in values if v is not None]
            out.append({'file_id':source[0],'sheet':source[1],'row_count':len(values),'valid_amount_count':len(known),'missing_or_invalid_count':len(values)-len(known),'sum':str(sum(known,Decimal(0)))})
        return {'items':out,'missing_is_zero':False}
    if action=='duplicates':
        groups={}
        for source in sources:
            for number,row in source[2]:
                if key(row):groups.setdefault(key(row),[]).append(evidence(source,number,row))
        items=[{'key':k,'sources':v} for k,v in groups.items() if len(v)>1]
    else:
        if len(sources)!=2:raise ValueError('two_files_required')
        groups=[]
        for source in sources:
            group={}
            for number,row in source[2]:
                if key(row):group.setdefault(key(row),[]).append((number,row))
            groups.append(group)
        items=[]
        for value in sorted(set(groups[0])|set(groups[1])):
            left,right=groups[0].get(value,[]),groups[1].get(value,[])
            refs=[evidence(sources[i],n,r) for i,g in enumerate((left,right)) for n,r in g]
            if len(left)>1 or len(right)>1:items.append({'key':value,'kind':'ambiguous_duplicate','sources':refs});continue
            if not left or not right:items.append({'key':value,'kind':'missing_left' if not left else 'missing_right','sources':refs});continue
            a,b=money(left[0][1]),money(right[0][1])
            if a is None or b is None:items.append({'key':value,'kind':'amount_unknown','sources':refs})
            elif a!=b:items.append({'key':value,'kind':'amount_difference','left_amount':str(a),'right_amount':str(b),'difference':str(a-b),'sources':refs})
    return {'items':items[:200],'total_items':len(items),'truncated':len(items)>200,'candidate_only':True,'identity_note':'按指定列精确匹配；同单不同费用、承担方、币种须另行核对。差值只表示数值不同，不足以判断谁多收、谁少付或最终责任，不自动改账。'}
