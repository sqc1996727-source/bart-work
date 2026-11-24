#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 2026-11-12 格式标准，从 workbench.html 里真实生成对账单 xlsx 与发车清单 xlsx，
落盘后用 verify_format.py 校验。

用法：
    python3 gen_sample.py [输出目录]
默认输出到 本目录/_samples/
"""
import base64
import html as htmllib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent
_CAND = [pathlib.Path("/Users/zhuzhu/Downloads/巨龙交付包/app/workbench.html"),
         pathlib.Path("/Users/zhuzhu/Downloads/julong-demo-v9.html")]
APP = next((p for p in _CAND if p.exists()), _CAND[0])
CHROME_CANDS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

HEAD = """<script>
window.__blobs=[];window.__errors=[];
const __oc=URL.createObjectURL.bind(URL);
URL.createObjectURL=function(b){window.__blobs.push(b);return __oc(b)};
window.addEventListener('error',e=>window.__errors.push(String(e.message)));
window.print=function(){};
try{['julong_demo_waybills_v2','julong_demo_recipients_v2','julong_demo_dispatch_v5',
'julong_demo_month_v1','julong_demo_fees_v1','julong_demo_receipts_v1','julong_demo_bills_v1',
'julong_demo_diffs_v1','julong_demo_delivery_v1','julong_demo_ai_v1']
.forEach(k=>localStorage.removeItem(k))}catch(e){}
</script>
"""

TAIL = """
<pre id="gen-report" style="display:none"></pre>
<script>
window.addEventListener('load',()=>setTimeout(async()=>{
 const $=id=>document.getElementById(id);
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const out={};
 function submit(n,date,dest,qty,w,amt,recv,phone,addr){
  $('number').value=n;$('date').value=date;
  $('receiver').value=recv;$('phone').value=phone;$('address').value=addr;
  $('destination').value=dest;$('cargo').value='染料';$('qty').value=qty;
  $('weight').value=w;$('freightTotal').value=String(amt);$('remark').value='';
  $('carrier').value='子铭物流';
  $('waybill-form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
 }
 async function b64(blob){
  const buf=new Uint8Array(await blob.arrayBuffer());
  let s='';const CH=0x8000;
  for(let i=0;i<buf.length;i+=CH)s+=String.fromCharCode.apply(null,buf.subarray(i,i+CH));
  return btoa(s);
 }
 try{
  await sleep(200);
  submit('0001401','2026-11-11','奉贤区','4托','0.95',829,'宁金凤','021-57540509','上海市奉贤区平海路966号（硕森仓库）');
  await sleep(60);
  submit('0001402','2026-11-11','浦东区','5托','2',1480,'张伟','021-58120033','上海市浦东新区川沙路8号');
  await sleep(60);
  submit('0001403','2026-11-12','嘉定区','9托','2.1',1542,'李强','021-69531122','上海市嘉定区博乐路80号');
  await sleep(80);

  const list=window.__JL.docs;const B=window.__JL.builders;
  const gs=B.billGroups();
  out.groups=gs.map(g=>({key:g.key,n:g.docs.length}));
  out.billName=B.billName(gs[0].docs,gs[0].key);
  out.billB64=await b64(B.makeBillXlsx(gs[0].docs));

  const zipDocs=list.filter(x=>x.carrier==='子铭物流');
  const g={date:zipDocs[0].date,partner:'子铭物流',docs:zipDocs};
  out.docName=B.fclName(g);
  out.docB64=await b64(B.makeFclXlsx(g));

  // 信封：渲染一票，交回字段与字号，供人工核对套印位置
  const dim={w:229,h:162};
  const env=B.makeEnvelope(zipDocs[0],dim);
  out.env=[...env.querySelectorAll('.env-f')].map(e=>({
   t:e.textContent, top:e.style.top, left:e.style.left, pt:e.style.fontSize}));
  out.errors=window.__errors;
 }catch(e){out.fatal=String(e&&e.stack||e)}
 document.getElementById('gen-report').textContent=JSON.stringify(out);
},250));
</script>
"""


def find_chrome():
    for p in CHROME_CANDS:
        if pathlib.Path(p).exists():
            return p
    return None


def main():
    outdir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "_samples"
    outdir.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("找不到 Chrome / Edge")

    src = APP.read_text(encoding="utf-8")
    i = src.find("<body")
    src = src[:i] + HEAD + src[i:]
    j = src.rfind("</body>")
    html = src[:j] + TAIL

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        tmp = f.name
    try:
        out = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=20000", "--dump-dom", f"file://{tmp}"],
            capture_output=True, text=True, timeout=180,
        ).stdout
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)

    m = re.search(r'<pre id="gen-report"[^>]*>(.*?)</pre>', out, re.S)
    if not m:
        raise SystemExit("页面没吐出结果，可能开头就报错了")
    data = json.loads(htmllib.unescape(m.group(1).strip()))
    if data.get("fatal"):
        raise SystemExit("页面内报错：\n" + data["fatal"][:1500])

    print("月份分组：", data["groups"])
    print("页面 JS 错误：", data.get("errors") or "无")

    bill = outdir / data["billName"]
    bill.write_bytes(base64.b64decode(data["billB64"]))
    doc = outdir / data["docName"]
    doc.write_bytes(base64.b64decode(data["docB64"]))
    print(f"对账单：{bill}  {bill.stat().st_size} B")
    print(f"发车清单：{doc}  {doc.stat().st_size} B")
    print("信封字段：")
    for e in data["env"]:
        print(f"   {e['pt']:>6}  top={e['top']:<5} left={e['left']:<5} {e['t']}")
    print()
    print("—— 接着跑格式校验 ——")
    for p in (bill, doc):
        subprocess.run([sys.executable, str(BASE / "verify_format.py"), str(p)])


if __name__ == "__main__":
    main()
