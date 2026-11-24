#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把发车清单 xlsx 按「打印出来的样子」画成 HTML 再截图，用于肉眼核对版式。

A4 横向 + 打印缩放（默认 75%）+ 页边距，全部从文件里读，不写死。
"""
import base64
import pathlib
import subprocess
import sys
import tempfile

import openpyxl

MM_PER_PX = 25.4 / 96
PT_PER_COL_CHAR = 7


def col_mm(width_chars, scale):
    """Excel 列宽（字符）→ 打印后的毫米数。"""
    px = width_chars * PT_PER_COL_CHAR + 5
    return px * MM_PER_PX * scale


def main():
    path = pathlib.Path(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fcl.png"
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    scale = (ws.page_setup.scale or 100) / 100
    paper = "A4"
    landscape = (ws.page_setup.orientation or "portrait") == "landscape"
    pw, ph = (297, 210) if landscape else (210, 297)
    m = ws.page_margins
    pad_l, pad_r = (m.left or 0) * 25.4, (m.right or 0) * 25.4
    pad_t = (m.top or 0) * 25.4

    ncol, nrow = ws.max_column, ws.max_row
    widths = []
    for c in range(1, ncol + 1):
        letter = openpyxl.utils.get_column_letter(c)
        w = ws.column_dimensions[letter].width or 9
        widths.append(col_mm(w, scale))
    total = sum(widths)

    def cell_html(cell, letter, ri, rn):
        v = cell.value
        txt = "" if v is None else str(v)
        b = cell.border
        css = []
        for side, prop in ((b.left, "left"), (b.right, "right"),
                           (b.top, "top"), (b.bottom, "bottom")):
            if side and side.style:
                css.append(f"border-{prop}:{0.75*scale:.2f}pt solid #000")
        if not css:
            css.append("border:0")
        al = (cell.alignment.horizontal or "center") if cell.alignment else "center"
        size = ((cell.font.sz or 10) * scale)
        weight = 700 if (cell.font and cell.font.b) else 400
        css.append(f"font-size:{size:.2f}pt;font-weight:{weight};text-align:{al}")
        cls = "" if al == "center" else ""
        return f'<td style="{";".join(css)}">{txt}</td>'

    rows = []
    merged = {}
    for rng in ws.merged_cells.ranges:
        merged[(rng.min_row, rng.min_col)] = (rng.max_col - rng.min_col + 1,
                                              rng.max_row - rng.min_row + 1)
    for r in range(1, nrow + 1):
        ht = (ws.row_dimensions[r].height or 18) * scale
        tds = []
        c = 1
        while c <= ncol:
            cover = merged.get((r, c))
            cell = ws.cell(row=r, column=c)
            if cover:
                span, _ = cover
                css_extra = f" colspan={span}"
                b = cell.border
                css = []
                for side, prop in ((b.left, "left"), (b.right, "right"),
                                   (b.top, "top"), (b.bottom, "bottom")):
                    if side and side.style:
                        css.append(f"border-{prop}:{0.75*scale:.2f}pt solid #000")
                css.append(f"font-size:{(cell.font.sz or 10)*scale:.2f}pt;"
                           f"font-weight:{700 if cell.font.b else 400};text-align:center")
                tds.append(f'<td colspan="{span}" style="{";".join(css)}">'
                           f'{cell.value or ""}</td>')
                c += span
                continue
            letter = openpyxl.utils.get_column_letter(c)
            tds.append(cell_html(cell, letter, r, nrow))
            c += 1
        rows.append(f'<tr style="height:{ht:.2f}pt">' + "".join(tds) + "</tr>")

    html = f"""<html><head><meta charset="utf-8"><style>
    @page {{ size: {paper} {'landscape' if landscape else 'portrait'}; margin: 0 }}
    body {{ margin:0; background:#e9edf1; font-family:"Microsoft YaHei","PingFang SC",sans-serif }}
    .paper {{ width:{pw}mm; height:{ph}mm; background:#fff; box-sizing:border-box;
              padding:{pad_t:.2f}mm {pad_r:.2f}mm 0 {pad_l:.2f}mm; margin:0 auto;
              box-shadow:0 2px 14px rgba(0,0,0,.15) }}
    table {{ border-collapse:collapse; table-layout:fixed; width:{total:.2f}mm }}
    td {{ vertical-align:middle; padding:0 1.2mm; overflow:hidden; white-space:nowrap;
          font-family:"Microsoft YaHei",sans-serif }}
    </style></head><body><div class="paper"><table>
    {''.join(f'<col style="width:{w:.2f}mm">' for w in widths)}
    {''.join(rows)}</table></div></body></html>"""
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--virtual-time-budget=4000", "--window-size=1200,860",
                    f"--screenshot={out}", "file://" + f.name],
                   capture_output=True, text=True, timeout=120)
    print("table width", round(total, 1), "mm / paper", pw, "mm  scale", scale)
    print("shot", out, pathlib.Path(out).stat().st_size if pathlib.Path(out).exists() else "MISS")


if __name__ == "__main__":
    main()
