#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巨龙输出格式校验（2026-11-12 标准）

用法：
    python3 verify_format.py <文件> [<文件> ...]
    python3 verify_format.py --std-only            # 只校验三份基准原件是否与标准一致

支持 .xlsx（对账单 / 发车清单）、.xls（信封套印模板 / 发车清单原件）。
2026-11-12 起停用「发货清单 .docx」，公司改用 Excel 发车清单。
只用标准库 + xlrd（仅 .xls 需要）。
退出码 0 = 全部通过；1 = 有不合格项。
"""
import sys
import os
import zipfile
import re
from xml.etree import ElementTree as ET

NS_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NS_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

# ---------------- 标准常量 ----------------
BILL_COLUMNS = ['到站', '货物名称', '数量', '重量', '运费', '进仓费', '单号', '发货日期', '备注']
BILL_COL_WIDTHS = {'A': 9, 'B': 9, 'C': 11.4, 'D': 9, 'H': 11.5, 'I': 9}
BILL_HEADER_ROW_HEIGHT = 28.0
BILL_FONT_PT = 11.0
BILL_PAPER_SIZE = 9  # A4
BILL_ORIENTATION = 'portrait'
BILL_LAST_ROW = 130        # 原件带边框的最后一行（表头 1 + 129 行）
BILL_DYE_ROW = 125         # B 列「染料」预填到这一行

# 发车清单（2026-11-12 第二份基准：26.10.30孙立伟车发车清单.xls）
FCL_COLUMNS = ['运单号', '开单日期', '发站', '到站', '收货人', '品名', '件数', '重量',
               '回付方式', '付货方式', '收货电话', '备注', '收货地址']
FCL_COL_WIDTHS = [12.63, 9.8, 8.13, 9, 9.88, 8.13, 11, 10.5, 8.38, 11.75, 18, 15.13, 51.25]
FCL_TITLE = '鞍山巨龙物流发货清单'
FCL_ROW_HEIGHT = 27.0
FCL_FONT = '微软雅黑'
FCL_FONT_PT = 10.0
FCL_TITLE_PT = 11.0
FCL_SCALE = 75
FCL_MARGINS = {'left': 0.51, 'right': 0.25, 'top': 0.94, 'bottom': 0.75}
FCL_ORIGIN = '鞍山'
FCL_NOTE_PREFIX = '回付'
FCL_FIRST_FILLED_ROW = 4          # 表头在第 3 行，数据从第 4 行起

SHIP_COLUMNS = ['到站', '货物名称', '数量', '重量', '付款方式', '付货方式', '联系方式', '备注']
SHIP_COL_WIDTHS = [1228, 1500, 1199, 1356, 1644, 1453, 1895, 1025]
SHIP_TABLE_WIDTH = 11300
SHIP_PAGE_W, SHIP_PAGE_H = 11906, 16838
SHIP_MARGINS = {'top': 400, 'right': 300, 'bottom': 284, 'left': 300}
SHIP_CELL_SZ = 30          # 半磅 -> 15pt
SHIP_TITLE_SZ = 28         # 14pt
SHIP_PHONE_SZ = 48         # 24pt
SHIP_HEADER_ROW_HEIGHT = 750
SHIP_HEADER_ROW_HEIGHT_OLD = 637
SHIP_MARGINS_OLD = {'top': 238, 'right': 567, 'bottom': 284, 'left': 567}
SHIP_BORDER_SZ = 4
SHIP_OUTER_SZ = 12          # 外框 1.5pt
SHIP_HEAD_RULE_SZ = 12      # 表头下框线 1.5pt
SHIP_FIXED_PAY_MODE = '回 执'
SHIP_FIXED_DELIVERY = '送'

ENV_FIELDS = {
    (0, 10): ('单号', 20.0),
    (1, 8): ('送', 12.0),
    (4, 5): ('收货地址', 16.0),
    (6, 5): ('收货人', 16.0),
    (9, 5): ('货物名称', 16.0),   # 原件在 7 行；兼容 9 行写法
}
ENV_FONT_NAME = '宋体'


class Result:
    def __init__(self):
        self.items = []

    def ok(self, name, detail=''):
        self.items.append(('OK', name, detail))

    def bad(self, name, detail=''):
        self.items.append(('FAIL', name, detail))

    def warn(self, name, detail=''):
        self.items.append(('WARN', name, detail))

    @property
    def failed(self):
        return [i for i in self.items if i[0] == 'FAIL']

    def dump(self, title):
        print('=' * 56)
        print(title)
        print('=' * 56)
        for st, name, detail in self.items:
            mark = {'OK': '  ok  ', 'FAIL': ' FAIL ', 'WARN': ' warn '}[st]
            print(f'[{mark}] {name}' + (f' — {detail}' if detail else ''))
        print(f'—— {len([i for i in self.items if i[0]=="OK"])} 通过 / '
              f'{len(self.failed)} 失败 / '
              f'{len([i for i in self.items if i[0]=="WARN"])} 提醒')


# ---------------- xlsx：厂家对账单 ----------------
def check_xlsx(path, r):
    z = zipfile.ZipFile(path)
    sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
    styles = ET.fromstring(z.read('xl/styles.xml'))
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        t = ET.fromstring(z.read('xl/sharedStrings.xml'))
        shared = [''.join(x.text or '' for x in si.iter(NS_S + 't'))
                  for si in t.findall(NS_S + 'si')]

    # 纸张
    ps = sheet.find(NS_S + 'pageSetup')
    if ps is None:
        r.warn('纸张设置', '未声明 pageSetup，Excel 默认可能是 Letter')
    else:
        paper = ps.get('paperSize')
        orient = ps.get('orientation', 'portrait')
        r.ok('纸张 A4', f'paperSize={paper}') if paper == str(BILL_PAPER_SIZE) \
            else r.bad('纸张 A4', f'paperSize={paper}，应为 {BILL_PAPER_SIZE}')
        r.ok('纵向', orient) if orient == BILL_ORIENTATION \
            else r.bad('纵向', f'orientation={orient}，应为 portrait')

    # 列宽
    colmap = {}
    cols_el = sheet.find(NS_S + 'cols')
    if cols_el is not None:
        for c in cols_el.findall(NS_S + 'col'):
            for i in range(int(c.get('min')), int(c.get('max')) + 1):
                colmap[i] = float(c.get('width'))
    letters = 'ABCDEFGHI'
    for idx, letter in enumerate(letters, start=1):
        want = BILL_COL_WIDTHS.get(letter)
        got = colmap.get(idx)
        if want is None:
            continue
        if got is None:
            r.warn(f'列宽 {letter}', '未显式设置，用默认宽度')
        elif abs(got - want) < 0.35:
            r.ok(f'列宽 {letter}', f'{got:g}')
        else:
            r.bad(f'列宽 {letter}', f'{got:g}，应约 {want}')

    # 表头
    rows = list(sheet.iter(NS_S + 'row'))
    if not rows:
        r.bad('工作表内容', '没有任何行')
        return

    def cell_text(c):
        v = c.find(NS_S + 'v')
        isel = c.find(NS_S + 'is')
        if c.get('t') == 's' and v is not None:
            return shared[int(v.text)]
        if isel is not None:
            return ''.join(x.text or '' for x in isel.iter(NS_S + 't'))
        return v.text if v is not None else ''

    header = []
    for c in rows[0].findall(NS_S + 'c'):
        header.append(cell_text(c).strip())
    while header and header[-1] == '':
        header.pop()
    if header == BILL_COLUMNS:
        r.ok('九列列名与列序', ' / '.join(header))
    else:
        r.bad('九列列名与列序', f'实际 {header}，应为 {BILL_COLUMNS}')

    ht = rows[0].get('ht')
    if ht is None:
        r.warn('表头行高', '未设置')
    elif abs(float(ht) - BILL_HEADER_ROW_HEIGHT) < 0.6:
        r.ok('表头行高', f'{ht}')
    else:
        r.bad('表头行高', f'{ht}，应 {BILL_HEADER_ROW_HEIGHT:g}')

    # 字体字号（取第 1 个数据行任一格）
    fonts = []
    for f in styles.find(NS_S + 'fonts').findall(NS_S + 'font'):
        sz = f.find(NS_S + 'sz')
        nm = f.find(NS_S + 'name')
        fonts.append((float(sz.get('val')) if sz is not None else None,
                      nm.get('val') if nm is not None else '',
                      f.find(NS_S + 'b') is not None))
    xfs = styles.find(NS_S + 'cellXfs').findall(NS_S + 'xf')
    data_rows = rows[1:]
    if data_rows:
        c = data_rows[0].findall(NS_S + 'c')
        if c:
            sidx = int(c[0].get('s') or 0)
            fid = int(xfs[sidx].get('fontId') or 0)
            sz, name, bold = fonts[fid]
            if sz is not None and abs(sz - BILL_FONT_PT) < 0.1:
                r.ok('正文字号', f'{sz:g}pt')
            else:
                r.bad('正文字号', f'{sz}pt，应 {BILL_FONT_PT:g}pt')
            if name == '宋体':
                r.ok('正文字体', name)
            else:
                r.bad('正文字体', f'{name}，应 宋体')
            if bold:
                r.warn('正文加粗', '标准不加粗')
    else:
        r.warn('数据行', '除表头外没有数据行')

    # 单号列必须文本、日期列必须真日期
    def col_letter(ref):
        m = re.match(r'([A-Z]+)', ref or '')
        return m.group(1) if m else ''

    numfmts = {}
    nf = styles.find(NS_S + 'numFmts')
    if nf is not None:
        for n in nf.findall(NS_S + 'numFmt'):
            numfmts[int(n.get('numFmtId'))] = n.get('formatCode')

    g_ok = g_total = h_ok = h_total = 0
    for row in data_rows[:200]:
        for c in row.findall(NS_S + 'c'):
            letter = col_letter(c.get('r'))
            if letter == 'G':
                v = c.find(NS_S + 'v')
                inl = c.find(NS_S + 'is')
                if (v is None or v.text in (None, '')) and inl is None:
                    continue
                g_total += 1
                txt = cell_text(c)
                if c.get('t') == 's' or (c.get('t') == 'inlineStr'):
                    if re.fullmatch(r'\d{7}', txt or ''):
                        g_ok += 1
            elif letter == 'H':
                v = c.find(NS_S + 'v')
                if v is None or v.text in (None, ''):
                    continue
                h_total += 1
                sidx = int(c.get('s') or 0)
                fmt = xfs[sidx].get('numFmtId') or '0'
                is_date = str(fmt) in ('14', '15', '16', '17', '22') or \
                    re.search(r'[ymd]', numfmts.get(int(fmt), '') or '') is not None
                if is_date and c.get('t') in (None, 'n'):
                    h_ok += 1
    if g_total:
        if g_ok == g_total:
            r.ok('单号为文本 7 位前导零', f'{g_ok}/{g_total} 格')
        else:
            r.bad('单号为文本 7 位前导零', f'{g_ok}/{g_total} 格合格，其余会被 Excel 吃掉前导零')
    else:
        r.warn('单号列', '没有数据')
    if h_total:
        if h_ok == h_total:
            r.ok('发货日期为真日期', f'{h_ok}/{h_total} 格')
        else:
            r.bad('发货日期为真日期', f'{h_ok}/{h_total} 格合格，其余是字符串')
    else:
        r.warn('发货日期列', '没有数据')

    # 表格有效行数：最后一行带边框的行（原件到第 130 行）
    xfs = styles.find(NS_S + 'cellXfs')
    xf_list = xfs.findall(NS_S + 'xf') if xfs is not None else []

    def bordered(c):
        sv = c.get('s')
        if sv is None:
            return False
        i = int(sv)
        return i < len(xf_list) and xf_list[i].get('borderId', '0') not in ('0', None)

    last_bordered = 0
    for row in rows:
        if any(bordered(c) for c in row.findall(NS_S + 'c')):
            try:
                last_bordered = max(last_bordered, int(row.get('r')))
            except (TypeError, ValueError):
                pass
    if last_bordered == BILL_LAST_ROW:
        r.ok('表格有效行数', f'到第 {last_bordered} 行')
    elif last_bordered:
        r.bad('表格有效行数', f'到第 {last_bordered} 行，应到第 {BILL_LAST_ROW} 行')
    else:
        r.warn('表格有效行数', '读不到带边框的行')

    # 每行九列都要有单元格，否则空白格没有边框，打印出来是缺格的表
    short = []
    for row in rows:
        rn = int(row.get('r'))
        if rn > last_bordered:
            continue
        letters = {re.sub(r'\d', '', c.get('r') or '') for c in row.findall(NS_S + 'c')}
        missing = [L for L in 'ABCDEFGHI' if L not in letters]
        if missing:
            short.append(f'{rn}行缺{"".join(missing)}')
    if not short:
        r.ok('九列逐格齐备', '空列也写了单元格，边框连续')
    else:
        r.bad('九列逐格齐备', f'{len(short)} 行缺格：' + '、'.join(short[:5]) + ('…' if len(short) > 5 else ''))

    # 空白行 B 列预填「染料」到第 125 行，126 行起只留框（原件就是这样）
    def b_of(rn):
        row = next((x for x in rows if int(x.get('r')) == rn), None)
        if row is None:
            return None
        for c in row.findall(NS_S + 'c'):
            if (c.get('r') or '') == f'B{rn}':
                return cell_text(c).strip()
        return None

    pre_ok = all(b_of(n) == '染料' for n in (BILL_DYE_ROW, BILL_DYE_ROW - 1))
    tail_blank = all(not (b_of(n) or '') for n in (BILL_DYE_ROW + 2, BILL_LAST_ROW))
    if pre_ok and tail_blank:
        r.ok('预填「染料」到 125 行', '126 行起留白框')
    elif pre_ok:
        r.warn('预填「染料」到 125 行', '已预填，但尾部多填了')
    else:
        r.warn('预填「染料」到 125 行', f'125 行 B 列是 {b_of(BILL_DYE_ROW)!r}')

    # 打印可用性（原件没有，但打多页时必须每页有表头）
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    titles = [d.text or '' for d in wb.iter(NS_S + 'definedName')
              if (d.get('name') or '').endswith('Print_Titles')]
    if titles:
        r.ok('打印标题（每页重复表头）', titles[0])
    else:
        r.warn('打印标题（每页重复表头）', '未设置，打多页时第二页起没有表头')

    pane = sheet.find(f'{NS_S}sheetViews/{NS_S}sheetView/{NS_S}pane')
    if pane is not None and pane.get('state') == 'frozen':
        r.ok('冻结首行', '滚到下面表头不跑')
    else:
        r.warn('冻结首行', '未冻结')


# ---------------- xlsx：发车清单（横向宽表） ----------------
def check_fcl(path, r):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    if 'xl/worksheets/sheet1.xml' not in names:
        r.bad('结构', '没有 sheet1.xml')
        return
    sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
    styles = ET.fromstring(z.read('xl/styles.xml'))
    book = ET.fromstring(z.read('xl/workbook.xml'))

    fonts = []
    for f in styles.find(NS_S + 'fonts').findall(NS_S + 'font'):
        sz = f.find(NS_S + 'sz')
        nm = f.find(NS_S + 'name')
        fonts.append((float(sz.get('val')) if sz is not None else None,
                      nm.get('val') if nm is not None else '',
                      f.find(NS_S + 'b') is not None))
    xfs = [{'font': int(x.get('fontId') or 0), 'border': int(x.get('borderId') or 0),
            'align': (x.find(NS_S + 'alignment').get('horizontal')
                      if x.find(NS_S + 'alignment') is not None else None)}
           for x in styles.find(NS_S + 'cellXfs').findall(NS_S + 'xf')]
    def side_style(bd, side):
        el = bd.find(NS_S + side)
        return el.get('style') if el is not None else None

    borders = [{k: side_style(b, full) for k, full in
                (('l', 'left'), ('r', 'right'), ('t', 'top'), ('b', 'bottom'))}
               for b in styles.find(NS_S + 'borders').findall(NS_S + 'border')]

    # 工作表名
    sheet_names = [x.get('name') for x in book.iter(NS_S + 'sheet')]
    if sheet_names and sheet_names[0] == '发车清单':
        r.ok('工作表名', sheet_names[0])
    else:
        r.warn('工作表名', f'{sheet_names}，基准是「发车清单」')

    # 纸张 / 方向 / 缩放 / 边距
    ps = sheet.find(NS_S + 'pageSetup')
    if ps is None:
        r.bad('打印设置', '缺 pageSetup')
    else:
        if ps.get('paperSize') == '9':
            r.ok('纸张 A4', 'paperSize=9')
        else:
            r.bad('纸张 A4', f'paperSize={ps.get("paperSize")}')
        if ps.get('orientation') == 'landscape':
            r.ok('横向', '基准是 A4 横向')
        else:
            r.bad('横向', f'orientation={ps.get("orientation")}')
        if ps.get('scale') == str(FCL_SCALE):
            r.ok('打印缩放', f'{FCL_SCALE}%（基准就是这个）')
        else:
            r.warn('打印缩放', f'{ps.get("scale")}，基准 {FCL_SCALE}%')
    pm = sheet.find(NS_S + 'pageMargins')
    if pm is not None:
        bad = [f'{k}={pm.get(k)}' for k, v in FCL_MARGINS.items()
               if abs(float(pm.get(k, 0)) - v) > 0.02]
        if bad:
            r.bad('页边距', '、'.join(bad) + f'，应 {FCL_MARGINS}')
        else:
            r.ok('页边距', '左0.51/右0.25/上0.94/下0.75 英寸')

    # 列宽
    cols = sheet.find(NS_S + 'cols')
    got = []
    if cols is not None:
        for c in cols.findall(NS_S + 'col'):
            got.append((int(c.get('min')), int(c.get('max')), float(c.get('width') or 0)))
    widths = []
    for i in range(1, 14):
        w = next((w for a, b, w in got if a <= i <= b), None)
        widths.append(w)
    if len([w for w in widths if w is not None]) == 13:
        bad = [f'{chr(64+i+1)}={w}' for i, w in enumerate(widths)
               if abs(w - FCL_COL_WIDTHS[i]) > 0.06]
        if bad:
            r.bad('13 列宽度', '；'.join(bad))
        else:
            r.ok('13 列宽度', '与基准逐列一致')
    else:
        r.bad('13 列宽度', f'只读到 {len([w for w in widths if w])} 列')

    # 行高
    rows = list(sheet.iter(NS_S + 'row'))
    bad_h = [row.get('r') for row in rows
             if abs(float(row.get('ht') or 0) - FCL_ROW_HEIGHT) > 0.2]
    if rows and not bad_h:
        r.ok('行高', f'{len(rows)} 行统一 {FCL_ROW_HEIGHT:g}pt')
    else:
        r.bad('行高', f'{len(bad_h)} 行不是 {FCL_ROW_HEIGHT:g}pt：{bad_h[:5]}')

    def cell(rn, letter):
        for row in rows:
            if row.get('r') != str(rn):
                continue
            for c in row.findall(NS_S + 'c'):
                if c.get('r') == f'{letter}{rn}':
                    return c
        return None

    def text(c):
        if c is None:
            return ''
        isel = c.find(NS_S + 'is')
        if isel is not None:
            return ''.join(x.text or '' for x in isel.iter(NS_S + 't'))
        v = c.find(NS_S + 'v')
        return v.text if v is not None else ''

    # 表头列名与列序
    head = [text(cell(3, chr(64 + i))).strip() for i in range(1, 14)]
    if head == FCL_COLUMNS:
        r.ok('表头 13 列', ' / '.join(head))
    else:
        diff = [f'{i+1}:{a!r}→{b!r}' for i, (a, b) in enumerate(zip(head, FCL_COLUMNS)) if a != b]
        r.bad('表头 13 列', '；'.join(diff[:4]) if diff else f'{head}')

    # 标题
    title = text(cell(1, 'A')).strip()
    merges = [m.get('ref') for m in sheet.iter(NS_S + 'mergeCell')]
    if title == FCL_TITLE:
        r.ok('标题', title)
    else:
        r.bad('标题', f'{title!r}，应 {FCL_TITLE!r}')
    if 'A1:M1' in merges:
        r.ok('标题合并', 'A1:M1')
    else:
        r.bad('标题合并', f'合并区 {merges}')

    # 字体
    t_xf = xfs[int(cell(1, 'A').get('s') or 0)]
    t_sz, t_name, t_bold = fonts[t_xf['font']]
    if t_name == FCL_FONT and abs((t_sz or 0) - FCL_TITLE_PT) < 0.1 and t_bold:
        r.ok('标题字号字体', f'{t_name} {t_sz:g}pt 加粗')
    else:
        r.bad('标题字号字体', f'{t_name} {t_sz}pt bold={t_bold}')
    body_xf = xfs[int(cell(FCL_FIRST_FILLED_ROW, 'A').get('s') or 0)]
    b_sz, b_name, b_bold = fonts[body_xf['font']]
    if b_name == FCL_FONT and abs((b_sz or 0) - FCL_FONT_PT) < 0.1 and b_bold:
        r.ok('正文字号字体', f'{b_name} {b_sz:g}pt 加粗')
    else:
        r.bad('正文字号字体', f'{b_name} {b_sz}pt bold={b_bold}')

    # 运单号：文本、七位前导零
    nums, bad_nums = 0, []
    rn = FCL_FIRST_FILLED_ROW
    while True:
        c = cell(rn, 'A')
        if c is None:
            break
        v = text(c).strip()
        if not v:
            break
        nums += 1
        if c.get('t') != 'inlineStr' and not v.startswith('0'):
            bad_nums.append(f'{rn}行{v}')
        elif v.isdigit():
            if len(v) != 7 or not v.startswith('0'):
                bad_nums.append(f'{rn}行{v}')
        rn += 1
    if nums and not bad_nums:
        r.ok('单号为文本', f'{nums} 票，七位前导零保留')
    elif nums:
        r.bad('单号为文本', '；'.join(bad_nums[:5]))
    else:
        r.warn('单号为文本', '没有数据行')

    # 对齐：地址列左，其余居中
    addr_xf = xfs[int(cell(FCL_FIRST_FILLED_ROW, 'M').get('s') or 0)]
    a_xf = xfs[int(cell(FCL_FIRST_FILLED_ROW, 'A').get('s') or 0)]
    if addr_xf['align'] == 'left' and a_xf['align'] == 'center':
        r.ok('对齐', '收货地址左对齐，其余居中')
    else:
        r.warn('对齐', f'地址 {addr_xf["align"]}，其余 {a_xf["align"]}')

    # 边框：数据区四边齐全
    last = int(rows[-1].get('r'))
    miss = []
    for rn in range(3, last + 1):
        for i in range(1, 14):
            c = cell(rn, chr(64 + i))
            if c is None:
                miss.append(f'{chr(64+i)}{rn}无格')
                continue
            bd = borders[xfs[int(c.get('s') or 0)]['border']]
            if not all(bd[k] for k in 'lrtb'):
                miss.append(f'{chr(64+i)}{rn}')
    if not miss:
        r.ok('边框', f'A3:M{last} 每格四边实线')
    else:
        r.bad('边框', f'{len(miss)} 格缺边：' + '、'.join(miss[:5]))

    # 标题行不该有下框线（底线由表头行提供，否则是双线）
    t_bd = borders[t_xf['border']]
    if not t_bd['b']:
        r.ok('标题下不加线', '避免与表头重叠成双线')
    else:
        r.warn('标题下不加线', '标题带了底框，打印会出现双线')

    # 底部：倒数第二行空行，最后一行日期 + 回付
    if not any(text(cell(last - 1, chr(64 + i))).strip() for i in range(1, 14)):
        r.ok('数据后留一空行', '手写补录用')
    else:
        r.warn('数据后留一空行', '倒数第二行有内容')
    date_cell = text(cell(last, 'K')).strip()
    if re.fullmatch(r'\d{2}/\d{2}/\d{2}日', date_cell):
        r.ok('底部日期', f'{date_cell}（K 列）')
    else:
        r.bad('底部日期', f'{date_cell!r}，格式应 26/11/11日')
    note = text(cell(last, 'M')).strip()
    if note.startswith(FCL_NOTE_PREFIX):
        if re.search(r'\d', note):
            r.bad('回付金额', f'{note!r} 带了金额；不许把厂家运费填进来')
        else:
            r.ok('回付金额', f'{note!r} 留手填')
    else:
        r.bad('回付金额', f'M{last} 是 {note!r}')

    # 价格保密：纸上不许出现运费/成本/利润字样
    alltext = ''.join(text(c) for c in sheet.iter(NS_S + 'c'))
    leaks = [k for k in ('运费', '成本', '利润', '厂家') if k in alltext]
    if leaks:
        r.bad('无内部字样', f'出现 {leaks}')
    else:
        r.ok('无内部字样', '不含运费 / 成本 / 利润 / 厂家')


# ---------------- xls（HTML 导出）：发车清单原件 ----------------
def check_fcl_ref(path, r):
    """基准原件是 WPS 导出的「HTML 装 xls」，不是 BIFF，直接按 HTML 量。"""
    raw = open(path, encoding='utf-8', errors='replace').read()
    if '<table' not in raw.lower():
        r.bad('结构', '不是 HTML 装的 xls，无法按基准量')
        return
    r.ok('文件形态', 'HTML 装的 .xls（WPS 导出）')
    if 'mso-page-orientation:landscape' in raw:
        r.ok('横向', '基准是 A4 横向')
    else:
        r.bad('横向', '基准声明不是横向')
    m = re.search(r'<x:Scale>(\d+)</x:Scale>', raw)
    if m:
        (r.ok if m.group(1) == str(FCL_SCALE) else r.warn)('打印缩放', f'{m.group(1)}%')
    m = re.search(r'@page\s*\{margin:([\d.]+)in ([\d.]+)in ([\d.]+)in ([\d.]+)in', raw)
    if m:
        top, right, bottom, left = m.groups()
        r.ok('页边距', f'上{top}/右{right}/下{bottom}/左{left} 英寸')

    body = raw[raw.lower().index('<table'):]
    rows = re.findall(r'<tr[^>]*>.*?</tr>', body, re.S)
    heights = {round(float(h), 2) for h in re.findall(r'height:([\d.]+)pt', body)}
    if heights == {FCL_ROW_HEIGHT}:
        r.ok('行高', f'全部 {FCL_ROW_HEIGHT:g}pt')
    else:
        r.warn('行高', f'基准行高集合 {sorted(heights)}')

    def cells(html):
        out = []
        for td in re.findall(r'<td[^>]*>.*?</td>', html, re.S):
            cs = re.search(r'colspan="?(\d+)"?', td[:80])
            span = int(cs.group(1)) if cs else 1
            txt = re.sub(r'<[^>]+>', '', td).replace('&nbsp;', ' ').strip()
            out.append((txt, span))
            out.extend([('', 1)] * (span - 1))
        return out

    grid = [cells(x) for x in rows]
    head = [t for t, _ in grid[2]][:13] if len(grid) > 2 else []
    if head == FCL_COLUMNS:
        r.ok('表头 13 列', ' / '.join(head))
    else:
        r.bad('表头 13 列', f'{head}')
    title = grid[0][0][0] if grid else ''
    if title == FCL_TITLE:
        r.ok('标题', title)
    else:
        r.bad('标题', f'{title!r}')
    # 司机行：名字在 J（第 10 列），电话在 K（第 11 列）
    if len(grid) > 1:
        row2 = [t for t, _ in grid[1]]
        if len(row2) > 10 and row2[9].startswith('司机'):
            r.ok('司机位置', f'J 列「{row2[9]}」，K 列「{row2[10]}」')
        else:
            r.warn('司机位置', f'第 2 行 {row2[:12]}')
    # 底部：日期在 K、回付在 M（取最后一条有内容的行，后面还有一堆空行）
    tail, tail_no = [], 0
    for i, g in enumerate(grid, 1):
        vals = [t for t, _ in g]
        if len(vals) > 12 and re.search(r'日$', vals[10]):
            tail, tail_no = vals, i
    if tail and tail[12].startswith('回付'):
        r.ok('底部位置', f'第 {tail_no} 行，K 列「{tail[10]}」，M 列「{tail[12]}」')
    else:
        r.warn('底部位置', f'{tail[:13] if tail else "没找到日期行"}')
    # 列宽（mso-width-alt/256 = 字符数）
    widths = [round(int(x) / 256, 2) for x in re.findall(r'mso-width-alt:(\d+)', body)
              if int(x) > 1000][:13]
    if len(widths) == 13:
        bad = [f'{chr(65+i)}={w}' for i, w in enumerate(widths)
               if abs(w - FCL_COL_WIDTHS[i]) > 0.06]
        if bad:
            r.warn('13 列宽度', '；'.join(bad))
        else:
            r.ok('13 列宽度', '与常量一致')


# ---------------- xls：信封套印模板 ----------------# ---------------- xls：信封套印模板 ----------------
def check_xls(path, r):
    try:
        import xlrd
    except ImportError:
        r.warn('信封校验', '未安装 xlrd，跳过')
        return
    b = xlrd.open_workbook(path, formatting_info=True)
    sh = b.sheet_by_index(0)
    if not sh.nrows:
        r.bad('信封内容', '空表')
        return

    found = {}
    for row in range(sh.nrows):
        for col in range(sh.ncols):
            v = sh.cell_value(row, col)
            if v in ('', None):
                continue
            xf = b.xf_list[sh.cell_xf_index(row, col)]
            font = b.font_list[xf.font_index]
            found[(row, col)] = (v, font.name, font.height / 20.0)

    def has(pred):
        return [(k, v) for k, v in found.items() if pred(str(v[0]))]

    if has(lambda s: re.fullmatch(r'\d{7}', s.strip())):
        k, v = has(lambda s: re.fullmatch(r'\d{7}', s.strip()))[0]
        r.ok('单号字段', f'({k[0]},{k[1]})={v[0]} {v[2]:g}pt')
        if abs(v[2] - 20.0) < 0.5:
            r.ok('单号字号', '20pt')
        else:
            r.warn('单号字号', f'{v[2]:g}pt，原件 20pt')
    else:
        r.bad('单号字段', '没有 7 位单号')

    if has(lambda s: s.strip() == '回 执'):
        r.ok('「回 执」字段', '含中间空格')
    else:
        r.bad('「回 执」字段', '未找到「回 执」')

    if has(lambda s: '区' in s or '市' in s) and has(lambda s: re.search(r'\d+号', s)):
        r.ok('收货地址字段', '含门牌号')
    else:
        r.warn('收货地址字段', '未识别到带门牌号的地址')

    sizes = {round(v[2], 1) for v in found.values()}
    if 16.0 in sizes:
        r.ok('正文字号 16pt', '大字体版')
    else:
        r.warn('正文字号', f'字号集合 {sorted(sizes)}，原件正文 16pt')

    names = {v[1] for v in found.values()}
    if names <= {'宋体', ''}:
        r.ok('字体', '宋体')
    else:
        r.bad('字体', f'{names}，应全宋体')

    # 禁止项：装饰性文案
    banned = ['厂家回执袋', '随货交付', '签收后返还', '内装厂家出具的回执']
    hit = [w for w in banned if any(w in str(v[0]) for v in found.values())]
    if hit:
        r.bad('无装饰文案', f'出现不该有的文字：{hit}')
    else:
        r.ok('无装饰文案', '纯套印')


CHECKERS = {
    '.xlsx': check_xlsx,
    '.xls': check_xls,
}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    base = os.path.dirname(os.path.abspath(__file__))
    targets = argv[1:]
    if targets == ['--std-only']:
        targets = [os.path.join(base, n) for n in
                   ['巨龙26年11月上海对账单.xlsx',
                    '26.10.30孙立伟车发车清单.xls',
                    '信封-大字体.xls']]
    total_fail = 0
    for path in targets:
        if not os.path.exists(path):
            print(f'找不到文件：{path}')
            total_fail += 1
            continue
        ext = os.path.splitext(path)[1].lower()
        base_name = os.path.basename(path)
        if ext == '.xlsx':
            fn = check_fcl if '车发车清单' in base_name else check_xlsx
        elif ext == '.xls' and '车发车清单' in base_name:
            fn = check_fcl_ref
        else:
            fn = CHECKERS.get(ext)
        r = Result()
        if fn is None:
            print(f'不支持的类型：{path}')
            total_fail += 1
            continue
        try:
            fn(path, r)
        except Exception as e:
            r.bad('解析', f'{type(e).__name__}: {e}')
        r.dump(f'{os.path.basename(path)}  ({os.path.getsize(path)} B)')
        print()
        total_fail += len(r.failed)
    print(f'总计失败项：{total_fail}')
    return 0 if total_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
