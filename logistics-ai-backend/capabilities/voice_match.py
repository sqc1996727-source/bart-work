"""Find phonetic candidates from already-authorized business records, never decide identity."""
import re

def candidates(text,rows,limit=12):
    try:from pypinyin import lazy_pinyin
    except ImportError:return []
    tokens=lazy_pinyin(str(text)[:2000],errors=lambda value:list(value))
    output=[];seen=set();fields=(("destination","到站"),("receiver","收货人"),("carrier","承运方"))
    for item in rows[:5000]:
        if not isinstance(item,dict):continue
        for field,label in fields:
            raw=str(item.get(field) or '').strip()
            if not re.fullmatch(r'[\u3400-\u9fff]{2,12}',raw) or raw in text:continue
            syllables=lazy_pinyin(raw);n=len(syllables)
            if not any(tokens[i:i+n]==syllables for i in range(len(tokens)-n+1)):continue
            number=str(item.get('number') or '')[:80]
            key=(field,raw,number)
            if key in seen:continue
            seen.add(key);output.append({'字段':label,'候选':raw,'运单号':number})
            if len(output)>=limit:return output
    return output
