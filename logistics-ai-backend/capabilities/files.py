from __future__ import annotations
import hashlib,io,json,mimetypes,os,secrets,subprocess,sys,zipfile,re
from pathlib import Path
from datetime import datetime

MAX_BYTES=10*1024*1024
EXTENSIONS={'.pdf','.xlsx','.xls','.csv','.tsv','.txt','.md','.docx','.png','.jpg','.jpeg','.webp','.wav','.m4a','.mp3','.ogg','.webm','.mp4'}
AUDIO={'.wav','.m4a','.mp3','.ogg','.webm','.mp4'}
RANK={'employee':0,'dispatcher':1,'owner_finance':2}

def initialize(host):
 with host.db() as con:
  con.execute('''CREATE TABLE IF NOT EXISTS agent_files(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,
    role_rank INTEGER NOT NULL,filename TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,created_at TEXT NOT NULL,source_ids TEXT NOT NULL DEFAULT '[]',extracted TEXT NOT NULL DEFAULT '')''')

def public(row):
 return {k:row[k] for k in ('id','filename','kind','sha256','size_bytes','created_at')}|{'url':'/api/agent/files/'+row['id'],'source_ids':json.loads(row['source_ids'])}

def authorized(host,user,file_id):
 from . import team
 with host.db() as con:
  row=con.execute('SELECT * FROM agent_files WHERE id=? AND tenant_id=?',(file_id,user['tenant_id'])).fetchone()
  if not row or RANK.get(user['role'],-1)<row['role_rank']:raise LookupError('not_found')
  if row['owner_id']==user['id']:return row
  shares=con.execute('SELECT number FROM case_files WHERE file_id=? AND tenant_id=? AND min_rank<=?',(file_id,user['tenant_id'],RANK[user['role']])).fetchall()
  if any(team.access(con,user,team.ticket(con,host,r['number'])) for r in shares):return row
  # Company publication is an explicit reader grant; private chats remain private.
  pubs=con.execute('SELECT source_ids FROM company_knowledge k WHERE tenant_id=? AND min_rank<=? AND (version=(SELECT max(version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic) OR version=(SELECT max(version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic AND v.valid_from<=?))',(user['tenant_id'],RANK[user['role']],datetime.now().astimezone().date().isoformat())).fetchall()
  if any(file_id in json.loads(p['source_ids']) for p in pubs):return row
 raise LookupError('not_found')

def path_for(host,row):
 root=(host.DATA_DIR/'agent_files').resolve();path=(host.DATA_DIR/row['path']).resolve()
 path.relative_to(root)
 if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise LookupError('file_unavailable')
 return path

def store(host,user,name,data,*,kind='upload',sources=(),text=''):
 ext=Path(name).suffix.lower()
 if ext not in EXTENSIONS|{'.zip'} or not data or len(data)>MAX_BYTES:raise ValueError('unsupported_or_oversized_file')
 filename=Path(name.replace('\\','/')).name[:150]
 # Stable provenance, private storage, no user paths/URLs accepted.
 digest=hashlib.sha256(data).hexdigest()
 with host.db() as con:
  old=con.execute('SELECT * FROM agent_files WHERE tenant_id=? AND owner_id=? AND sha256=? AND kind=? AND role_rank<=? LIMIT 1',(user['tenant_id'],user['id'],digest,kind,RANK[user['role']])).fetchone()
  if old:return public(old)|{'duplicate':True}
 root=host.DATA_DIR/'agent_files';root.mkdir(mode=0o700,parents=True,exist_ok=True)
 if root.is_symlink():raise ValueError('unsafe_storage')
 fid=secrets.token_urlsafe(18);dest=root/(fid+ext)
 with dest.open('xb') as out:os.chmod(dest,0o600);out.write(data)
 with host.db() as con:
  con.execute('INSERT INTO agent_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(fid,user['tenant_id'],user['id'],RANK[user['role']],filename,kind,str(dest.relative_to(host.DATA_DIR)),digest,len(data),host.now_iso(),json.dumps(list(sources)),text))
  host.audit(con,user,'agent.file.'+kind,'agent_file',fid,{'sha256':digest,'size_bytes':len(data),'source_count':len(sources)})
 return {'id':fid,'filename':filename,'kind':kind,'sha256':digest,'size_bytes':len(data),'created_at':host.now_iso(),'url':'/api/agent/files/'+fid,'source_ids':list(sources)}

def upload(host,user,name,data):
 ext=Path(name).suffix.lower()
 if ext not in EXTENSIONS:raise ValueError('unsupported_file_type')
 # Reject active Office containers and enforce basic structural signatures.
 if ext in {'.xlsx','.docx'}:
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   if len(z.infolist())>1500 or sum(x.file_size for x in z.infolist())>80*1024*1024:raise ValueError('expanded_file_too_large')
   if any('vbaproject' in x.filename.lower() or '..' in Path(x.filename).parts for x in z.infolist()):raise ValueError('unsafe_document')
 elif ext=='.pdf' and not data.startswith(b'%PDF-'):raise ValueError('invalid_pdf')
 elif ext in {'.png','.jpg','.jpeg','.webp'}:
  from PIL import Image
  Image.MAX_IMAGE_PIXELS=30_000_000
  with Image.open(io.BytesIO(data)) as image:image.verify()
 return store(host,user,name,data)

def extract(host,user,fid):
 row=authorized(host,user,fid)
 if row['kind']=='artifact' and row['extracted']:return json.loads(row['extracted'])
 if row['extracted']:return json.loads(row['extracted'])
 path=path_for(host,row)
 # Parser runs outside the HTTP/model process, with time/CPU limits.
 parser_python=str(Path(__file__).resolve().parents[1]/'.media-venv'/'bin'/'python') if path.suffix.lower() in AUDIO|{'.pdf','.png','.jpg','.jpeg','.webp'} else sys.executable
 proc=subprocess.run([parser_python,str(Path(__file__).resolve()),str(path)],capture_output=True,text=True,timeout=120,start_new_session=True,
    env={**{k:os.environ[k] for k in ('PATH','LANG','TMPDIR','JULONG_ASR_MODEL') if k in os.environ},'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1','TOKENIZERS_PARALLELISM':'false'})
 if proc.returncode:raise ValueError('file_read_failed')
 result=json.loads(proc.stdout)
 with host.db() as con:con.execute('UPDATE agent_files SET extracted=? WHERE id=?',(json.dumps(result,ensure_ascii=False),fid))
 return result

def parse_local(path):
 ext=path.suffix.lower();text='';locations=[];tables=[]
 if ext=='.pdf':
  from pypdf import PdfReader
  reader=PdfReader(path)
  if len(reader.pages)>150:raise ValueError('too_many_pages')
  for i,page in enumerate(reader.pages):
   value=(page.extract_text() or '')[:12000]
   if not value.strip():
    import pypdfium2 as pdfium
    from rapidocr_onnxruntime import RapidOCR
    import numpy as np
    with pdfium.PdfDocument(str(path)) as rendered:
     image=rendered[i].render(scale=1.5).to_pil()
     found,_=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2)(np.array(image))
     value='\n'.join(v for _,v,_ in found or [])
   locations.append({'page':i+1,'text':value});text+=f'\n[第{i+1}页]\n'+value
 elif ext in {'.xlsx','.xls'}:
  if ext=='.xlsx':
   from openpyxl import load_workbook
   workbook=load_workbook(path,read_only=True,data_only=True)
   def display(cell):
    value=cell.value
    if isinstance(value,(int,float)) and value==int(value) and cell.number_format and set(cell.number_format)=={'0'} and len(cell.number_format)>1:return str(int(value)).zfill(len(cell.number_format))
    return value
   sheets=((s.title,([display(c) for c in r] for r in s.iter_rows())) for s in workbook)
  else:
   import xlrd
   workbook=xlrd.open_workbook(path);sheets=((s.name,(s.row_values(i) for i in range(s.nrows))) for s in workbook.sheets())
  total=0
  for title,rows in sheets:
   cells=[]
   for i,row in enumerate(rows):
    total+=len(row)
    if i>=3000 or total>60000:raise ValueError('table_too_large')
    values=[str(v) if v is not None else '' for v in row[:80]];cells.append(values)
    text+=f'\n[{title}!行{i+1}] '+ '\t'.join(values)
   tables.append({'sheet':title,'rows':cells})
  if hasattr(workbook,'close'):workbook.close()
 elif ext=='.zip':
  with zipfile.ZipFile(path) as archive:text='资料包内文件：\n'+'\n'.join(archive.namelist()[:200])
 elif ext=='.docx':
  from docx import Document
  doc=Document(path);text='\n'.join(p.text for p in doc.paragraphs)
  for table in doc.tables:
   rows=[[c.text for c in r.cells] for r in table.rows];tables.append({'sheet':'表格','rows':rows});text+='\n'+'\n'.join('\t'.join(r) for r in rows)
 elif ext in {'.png','.jpg','.jpeg','.webp'}:
  from rapidocr_onnxruntime import RapidOCR
  values,_=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2)(str(path))
  for box,value,score in values or []:
   locations.append({'box':box,'text':value,'confidence':float(score)});text+=value+'\n'
 elif ext in AUDIO:
  from faster_whisper import WhisperModel
  model_path=Path(os.environ.get('JULONG_ASR_MODEL',str(Path(__file__).resolve().parents[1]/'models'/'whisper-tiny')))
  if not (model_path/'model.bin').exists():raise ValueError('speech_model_not_installed')
  model=WhisperModel(str(model_path),device='cpu',compute_type='int8',local_files_only=True,cpu_threads=4)
  segments,info=model.transcribe(str(path),language='zh',beam_size=3,vad_filter=True)
  for seg in segments:locations.append({'start':seg.start,'end':seg.end,'text':seg.text});text+=seg.text
 else:
  text=path.read_text(encoding='utf-8-sig',errors='replace')
  if ext in {'.csv','.tsv'}:
   import csv
   rows=list(csv.reader(io.StringIO(text),delimiter='\t' if ext=='.tsv' else ','))
   if len(rows)>10000:raise ValueError('table_too_large')
   tables=[{'sheet':'数据','rows':rows}]
 return {'text':text[:60000],'locations':locations[:2000],'tables':tables[:15],'truncated':len(text)>60000,'source_format':ext}

def make_artifact(host,user,args,key):
 kind=args.get('format','xlsx');ids=args.get('file_ids',[])
 if kind not in {'xlsx','csv','pdf','zip'} or not isinstance(ids,list) or len(ids)>12:raise ValueError('invalid_artifact')
 sources=[authorized(host,user,fid) for fid in ids]
 provenance=args.get('_provenance_ids',ids)
 if not isinstance(provenance,list) or len(provenance)>12:raise ValueError('invalid_sources')
 for fid in provenance:authorized(host,user,fid)
 title=str(args.get('title','业务资料'))[:80]
 rows=args.get('rows',[]);columns=args.get('columns',[]);body=str(args.get('text',''))[:30000]
 if not isinstance(rows,list) or len(rows)>1000 or not isinstance(columns,list) or len(columns)>40:raise ValueError('invalid_rows')
 if any(not isinstance(row,list) or len(row)>40 for row in rows):raise ValueError('invalid_rows')
 if len(json.dumps([columns,rows],ensure_ascii=False))>2_000_000:raise ValueError('document_too_large')
 out=io.BytesIO()
 if kind=='zip':
  if not sources:raise ValueError('files_required')
  with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
   for i,row in enumerate(sources):z.writestr(f'{i+1:02d}-{row["filename"]}',path_for(host,row).read_bytes())
 elif kind=='xlsx':
  from openpyxl import Workbook
  from openpyxl.styles import Font
  book=Workbook();sheet=book.active;sheet.title='整理结果'
  def safe(v):
   if isinstance(v,(int,float)):
    import math
    if not math.isfinite(v):raise ValueError('invalid_number')
    return v
   v=str(v) if v is not None else ''
   return "'"+v if v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r"-[0-9]+(?:\.[0-9]+)?",v.strip()) else v
  if columns:sheet.append([safe(x) for x in columns])
  for row in rows:sheet.append([safe(x) for x in row])
  if not rows and body:
   for line in body.splitlines():sheet.append([safe(line)])
  for row in sheet:
   for cell in row:cell.font=Font(name='宋体',size=11)
  book.save(out)
 elif kind=='csv':
  import csv
  buf=io.StringIO();writer=csv.writer(buf)
  def safe(v):
   v=str(v);return "'"+v if v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r"-[0-9]+(?:\.[0-9]+)?",v.strip()) else v
  if columns:writer.writerow([safe(x) for x in columns])
  writer.writerows([[safe(x) for x in row] for row in rows]);out.write(buf.getvalue().encode('utf-8-sig'))
 elif kind=='pdf':
  if sources and not body and not rows:
   from pypdf import PdfWriter
   from PIL import Image
   pdf=PdfWriter()
   for row in sources:
    path=path_for(host,row)
    if path.suffix.lower()=='.pdf':pdf.append(str(path))
    elif path.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}:
     image=Image.open(path).convert('RGB');b=io.BytesIO();image.save(b,format='PDF');b.seek(0);pdf.append(b)
    else:raise ValueError('pdf_sources_required')
   pdf.write(out)
  else:
   from reportlab.pdfgen import canvas
   from reportlab.pdfbase import pdfmetrics
   from reportlab.pdfbase.cidfonts import UnicodeCIDFont
   pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
   pdf=canvas.Canvas(out);pdf.setTitle(title);pdf.setFont('STSong-Light',13);y=800
   for line in [title,'']+body.splitlines()+([' / '.join(map(str,columns))] if columns else [])+[' / '.join(map(str,row)) for row in rows]:
    for start in range(0,max(1,len(line)),42):
     if y<55:pdf.showPage();pdf.setFont('STSong-Light',13);y=800
     pdf.drawString(40,y,line[start:start+42]);y-=20
   pdf.save()
 return store(host,user,title+'.'+kind,out.getvalue(),kind='artifact',sources=provenance)

if __name__=='__main__':
 if os.name=='posix':
  import resource
  resource.setrlimit(resource.RLIMIT_CPU,(90,90))
 try:print(json.dumps(parse_local(Path(sys.argv[1])),ensure_ascii=False,default=str))
 except Exception:sys.exit(2)
