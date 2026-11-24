# -*- coding: utf-8 -*-
"""把生成的对账单 xlsx 按真实版式画成 HTML，再用 Chrome 截图核对。
发车清单用 render_fcl.py（它按打印缩放算）。"""
import pathlib, subprocess, tempfile, os, zipfile, re
from xml.etree import ElementTree as ET
import openpyxl

BASE = pathlib.Path('/Users/zhuzhu/WorkBuddy/2026-11-09-21-28-22/julong-trial/reference-formats/_samples')
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MM2PX = 3.779527559

def shot(html, out, w, h):
    f = tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8')
    f.write(html); f.close()
    subprocess.run(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','--headless=new',
        '--disable-gpu','--no-sandbox','--hide-scrollbars','--force-device-scale-factor=2',
        '--virtual-time-budget=5000',f'--window-size={w},{h}',f'--screenshot={out}','file://'+f.name],
        capture_output=True, text=True, timeout=120)
    os.unlink(f.name)
    return pathlib.Path(out).exists()

# ---------- xlsx ----------
def render_xlsx(path, out, max_rows=18):
    ws = openpyxl.load_workbook(path).active
    widths = []
    for i in range(1, 10):
        letter = chr(64+i)
        w = ws.column_dimensions[letter].width or 9
        widths.append(round(w*7+5))
    rows_html = []
    for r in range(1, min(ws.max_row, max_rows)+1):
        h = 37 if r == 1 else 24
        cells = []
        for c in range(1, 10):
            cell = ws.cell(row=r, column=c)
            v = cell.value
            if hasattr(v, 'strftime'):
                v = v.strftime('%Y-%m-%d')
            txt = '' if v is None else str(v)
            cells.append(f'<td>{txt}</td>')
        rows_html.append(f'<tr style="height:{h}px">' + ''.join(cells) + '</tr>')
    cols = ''.join(f'<col style="width:{w}px">' for w in widths)
    # 读打印页眉页脚，还原成纸上看到的样子
    z = zipfile.ZipFile(path)
    shx = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
    hf = shx.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}headerFooter')

    def seg(txt, tag):
        i = txt.find(tag)
        if i < 0:
            return ''
        j = min([x for x in (txt.find('&L', i+2), txt.find('&C', i+2), txt.find('&R', i+2)) if x > 0] or [len(txt)])
        seg_txt = txt[i+2:j]
        return re.sub(r'&"[^"]*"', '', re.sub(r'&\d{1,2}', '', seg_txt))

    head_l = head_r = foot_c = ''
    if hf is not None:
        oh = hf.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}oddHeader')
        of = hf.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}oddFooter')
        if oh is not None and oh.text:
            head_l, head_r = seg(oh.text, '&L'), seg(oh.text, '&R')
        if of is not None and of.text:
            foot_c = seg(of.text, '&C').replace('&P', '1').replace('&N', '4')
    html = f'''<html><head><meta charset="utf-8"><style>
    body{{margin:0;background:#e9edf1;font-family:"Songti SC","SimSun",serif}}
    .sheet{{width:794px;background:#fff;padding:54px;box-sizing:border-box;margin:0 auto}}
    table{{border-collapse:collapse;table-layout:fixed;font-size:14.67px}}
    td{{border:1px solid #000;text-align:center;vertical-align:middle;font-family:"SimSun",serif;overflow:hidden;white-space:nowrap}}
    tr:first-child td{{border-top:2px solid #000;border-bottom:2px solid #000}}
    tr:last-child td{{border-bottom:2px solid #000}}
    table{{border-left:2px solid #000;border-right:2px solid #000}}
    .hd{{display:flex;justify-content:space-between;font-size:13.3px;font-family:"SimSun",serif;margin:0 0 8px}}
    .ft{{display:flex;justify-content:center;font-size:12px;font-family:"SimSun",serif;margin-top:10px}}
    </style></head><body><div class="sheet"><div class="hd"><span>{head_l}</span><span>{head_r}</span></div><table>{cols}{''.join(rows_html)}</table><div class="ft">{foot_c}</div></div></body></html>'''
    return shot(html, out, 794, 1000)

x = BASE/'巨龙26年11月上海对账单.xlsx'
print('xlsx', render_xlsx(x, '/tmp/fmt-bill.png'))
for p in ['/tmp/fmt-bill.png']:
    print(p, pathlib.Path(p).stat().st_size if pathlib.Path(p).exists() else 'MISS')
