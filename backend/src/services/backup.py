"""整册备份与恢复：一致性快照打包、HMAC签名校验、幂等恢复。

备份包为gzip tar（.swb）：
  manifest.json        格式/版本/时间/计数/逐文件SHA256/HMAC签名
  relations.json       草案（code为自然键）
  dependencies.json    依赖（按relation_code引用）
  reports.json         已完成报告（含冻结快照与结果证据）
  files/report-*.json  报告原始文件字节

恢复状态机：pending(待校验) -> verifying(校验中) -> restoring(恢复中) -> restored(已恢复)/failed。
任何损坏、缺件、篡改都在verifying阶段整包拒绝；restoring阶段单事务落库，
报告文件在commit前写好，commit失败即回滚并清理已写文件，不留半册。
"""
import hashlib,hmac,io,json,os,tarfile
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.config.database import Session
from src.config.settings import JWT_SECRET,ARTIFACTS_DIR
from src.models.entities import Relation,Dependency,Report,RestoreJob,Audit
from src.utils.serialization import record
from src.services.reports import queue

FORMAT='schemaworkbook-backup'
VERSION=1
MAX_PACKAGE_BYTES=20*1024*1024
MAX_RELATIONS=500
MAX_REPORTS=5000
RESTORE_QUEUE='schemaworkbook:restore-queue'
ACTIVE_STATES=('pending','verifying','restoring')

class PackageError(Exception):pass

def _canonical(obj):return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def _sha256(data):return hashlib.sha256(data).hexdigest()
def _sign(manifest):return hmac.new(JWT_SECRET.encode(),_canonical(manifest).encode(),hashlib.sha256).hexdigest()
def _iso(value):
    if isinstance(value,datetime):
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)
def _parse_dt(value):
    try:dt=datetime.fromisoformat(str(value))
    except Exception:raise PackageError('报告时间格式无效')
    if dt.tzinfo:dt=dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
def _artifacts():return Path(ARTIFACTS_DIR)
def _enqueue(job_id):queue.rpush(RESTORE_QUEUE,str(job_id))

# ---------- 备份导出 ----------

def build_backup(db,hook=None):
    """在db所在事务的一致性快照中打包整册；hook仅供测试在快照建立后注入并发修改。"""
    relations=list(db.scalars(select(Relation).order_by(Relation.id)))
    if hook:hook()
    dependencies=list(db.scalars(select(Dependency).order_by(Dependency.id)))
    reports=list(db.scalars(select(Report).where(Report.status=='completed').order_by(Report.id)))
    codes={r.id:r.code for r in relations}
    members={'relations.json':_canonical([{'code':r.code,'title':r.title,'attributes':r.attributes,'note':r.note or ''} for r in relations]).encode(),
             'dependencies.json':_canonical([{'relation_code':codes[d.relation_id],'lhs':d.lhs,'rhs':d.rhs,'note':d.note or ''} for d in dependencies]).encode()}
    entries=[]
    for rep in reports:
        fname=f'files/report-{rep.id}.json'
        path=_artifacts()/f'report-{rep.id}.json'
        data=path.read_bytes() if path.exists() else json.dumps(rep.result,ensure_ascii=False,indent=2).encode()
        members[fname]=data
        entries.append({'name':rep.name,'relation_code':codes[rep.relation_id],'input_snapshot':rep.input_snapshot,'result':rep.result,'created_at':_iso(rep.created_at),'file':fname})
    members['reports.json']=_canonical(entries).encode()
    manifest={'format':FORMAT,'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
              'counts':{'relations':len(relations),'dependencies':len(dependencies),'reports':len(reports)},
              'files':{name:_sha256(data) for name,data in sorted(members.items())}}
    manifest['signature']=_sign(manifest)
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for name,data in [('manifest.json',_canonical(manifest).encode())]+sorted(members.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mtime=0
            tar.addfile(info,io.BytesIO(data))
    package=buf.getvalue()
    return package,_sha256(package),manifest['counts']

# ---------- 包校验 ----------

def _valid_attrs(value,lo,hi):
    return isinstance(value,list) and lo<=len(value)<=hi and all(isinstance(x,str) and x.strip() and len(x)<=40 for x in value) and len(set(value))==len(value)

def verify_package(content):
    """校验并解析备份包；任何损坏、缺件、篡改都抛PackageError，整包拒绝。"""
    if not content:raise PackageError('备份包为空')
    try:
        with tarfile.open(fileobj=io.BytesIO(content),mode='r:gz') as tar:
            members={}
            for m in tar.getmembers():
                if not m.isfile():continue
                if m.name.startswith('/') or '..' in m.name.split('/'):raise PackageError('备份包含有非法文件路径')
                f=tar.extractfile(m);members[m.name]=f.read() if f else b''
    except PackageError:raise
    except Exception:raise PackageError('备份包已损坏，无法读取，整包拒绝')
    if 'manifest.json' not in members:raise PackageError('备份包缺少清单文件，整包拒绝')
    try:manifest=json.loads(members.pop('manifest.json'))
    except Exception:raise PackageError('清单文件已损坏，整包拒绝')
    if not isinstance(manifest,dict) or manifest.get('format')!=FORMAT or manifest.get('version')!=VERSION:raise PackageError('备份包格式不受支持，整包拒绝')
    signature=manifest.get('signature')
    signed={k:v for k,v in manifest.items() if k!='signature'}
    if not isinstance(signature,str) or not hmac.compare_digest(signature,_sign(signed)):raise PackageError('签名校验失败，备份包可能被篡改，整包拒绝')
    listed=manifest.get('files')
    if not isinstance(listed,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in listed.items()):raise PackageError('清单登记信息无效，整包拒绝')
    if set(members)!=set(listed):raise PackageError('备份包缺件或含有未登记文件，整包拒绝')
    for name,digest in listed.items():
        if _sha256(members[name])!=digest:raise PackageError(f'文件 {name} 校验失败，备份包可能被篡改或损坏，整包拒绝')
    try:
        relations=json.loads(members['relations.json']);dependencies=json.loads(members['dependencies.json']);reports=json.loads(members['reports.json'])
    except Exception:raise PackageError('备份包内容文件缺失或已损坏，整包拒绝')
    if not isinstance(relations,list) or len(relations)>MAX_RELATIONS:raise PackageError('草案清单无效，整包拒绝')
    codes=set();attrs_of={}
    for r in relations:
        if not isinstance(r,dict):raise PackageError('草案记录无效，整包拒绝')
        code,title,attrs,note=r.get('code'),r.get('title'),r.get('attributes'),r.get('note','')
        if not isinstance(code,str) or not 2<=len(code)<=80:raise PackageError('草案编号无效，整包拒绝')
        if code in codes:raise PackageError(f'草案编号 {code} 在包内重复，整包拒绝')
        codes.add(code)
        if not isinstance(title,str) or not 2<=len(title)<=160:raise PackageError(f'草案 {code} 名称无效，整包拒绝')
        if not _valid_attrs(attrs,1,10):raise PackageError(f'草案 {code} 属性必须唯一且为1–10项，整包拒绝')
        if not isinstance(note,str) or len(note)>300:raise PackageError(f'草案 {code} 备注无效，整包拒绝')
        attrs_of[code]=set(attrs)
    if not isinstance(dependencies,list):raise PackageError('依赖清单无效，整包拒绝')
    per={}
    for d in dependencies:
        if not isinstance(d,dict):raise PackageError('依赖记录无效，整包拒绝')
        rc,lhs,rhs,note=d.get('relation_code'),d.get('lhs'),d.get('rhs'),d.get('note','')
        if rc not in codes:raise PackageError('依赖引用了包外草案，整包拒绝')
        if not _valid_attrs(lhs,0,10) or not _valid_attrs(rhs,1,10):raise PackageError(f'草案 {rc} 的依赖属性无效，整包拒绝')
        if not set(lhs+rhs)<=attrs_of[rc]:raise PackageError(f'依赖引用草案 {rc} 之外的属性，整包拒绝')
        if not isinstance(note,str) or len(note)>300:raise PackageError(f'草案 {rc} 的依赖备注无效，整包拒绝')
        per[rc]=per.get(rc,0)+1
        if per[rc]>64:raise PackageError(f'草案 {rc} 依赖超过64条，整包拒绝')
    if not isinstance(reports,list) or len(reports)>MAX_REPORTS:raise PackageError('报告清单无效，整包拒绝')
    files={k:v for k,v in members.items() if k.startswith('files/')}
    referenced=set()
    for rep in reports:
        if not isinstance(rep,dict):raise PackageError('报告记录无效，整包拒绝')
        name,rc,snap,result,fname=rep.get('name'),rep.get('relation_code'),rep.get('input_snapshot'),rep.get('result'),rep.get('file')
        if not isinstance(name,str) or not 2<=len(name)<=120:raise PackageError('报告名称无效，整包拒绝')
        if rc not in codes:raise PackageError('报告引用了包外草案，整包拒绝')
        if not isinstance(snap,dict) or 'relation' not in snap or 'dependencies' not in snap:raise PackageError(f'报告 {name} 缺少冻结快照证据，整包拒绝')
        if not isinstance(result,dict):raise PackageError(f'报告 {name} 缺少结果证据，整包拒绝')
        _parse_dt(rep.get('created_at'))
        if not isinstance(fname,str) or fname not in files:raise PackageError(f'报告 {name} 缺少报告文件，整包拒绝')
        if fname in referenced:raise PackageError('报告文件被重复引用，整包拒绝')
        referenced.add(fname)
    if referenced!=set(files):raise PackageError('报告文件与报告记录不匹配，整包拒绝')
    return {'relations':relations,'dependencies':dependencies,'reports':reports,'files':files}

# ---------- 恢复执行 ----------

def _mark(job_id,status,error,sf):
    with sf() as db:
        job=db.get(RestoreJob,job_id)
        if job:job.status,job.error=status,error;db.commit()

def _cleanup_orphans(db):
    """清理上次恢复崩溃留下的孤儿报告文件（worker串行执行，分解任务先落库后写文件，安全）。"""
    known=set(db.scalars(select(Report.id)).all())
    for path in _artifacts().glob('report-*.json'):
        try:ident=int(path.stem.split('-')[1])
        except Exception:continue
        if ident not in known:path.unlink(missing_ok=True)

def run_restore(job_id,session_factory=None,fault=None):
    """执行恢复任务；可重入：已提交内容按stats跳过，重复执行不重复新增。fault仅供测试注入故障。"""
    sf=session_factory or Session
    fault=os.getenv('SCHEMAWORKBOOK_RESTORE_FAULT','') if fault is None else fault
    with sf() as db:
        job=db.get(RestoreJob,job_id)
        if not job or job.status!='pending':return
        job.status='verifying';db.commit()
        path,checksum=job.package_path,job.package_checksum
    try:content=Path(path).read_bytes()
    except Exception:_mark(job_id,'failed','备份包文件丢失，请重新上传',sf);return
    try:pkg=verify_package(content)
    except PackageError as e:_mark(job_id,'failed',str(e),sf);return
    except Exception:_mark(job_id,'failed','备份包校验异常，整包拒绝',sf);return
    written=[]
    try:
        with sf() as db:
            job=db.get(RestoreJob,job_id)
            job.status='restoring';db.commit()
            _artifacts().mkdir(parents=True,exist_ok=True)
            _cleanup_orphans(db)
            stats=job.stats or {'relation_map':{},'relations_skipped':[],'dependencies_added':0,'dependencies_skipped':0,'reports':[],'reports_skipped':0}
            rel_map=dict(stats['relation_map']);skipped=set(stats['relations_skipped']);new_codes=set()
            for entry in pkg['relations']:
                code=entry['code']
                if code in rel_map or code in skipped:continue
                if db.scalar(select(Relation).where(Relation.code==code)):skipped.add(code)
                else:
                    obj=Relation(code=code,title=entry['title'],attributes=entry['attributes'],note=entry.get('note',''))
                    db.add(obj);db.flush();rel_map[code]=obj.id;new_codes.add(code)
            deps_added,deps_skipped=stats['dependencies_added'],stats['dependencies_skipped']
            for entry in pkg['dependencies']:
                rc=entry['relation_code']
                if rc in skipped:deps_skipped+=1;continue
                if rc not in new_codes:continue
                db.add(Dependency(relation_id=rel_map[rc],lhs=entry['lhs'],rhs=entry['rhs'],note=entry.get('note','')))
                deps_added+=1
            reports,reports_skipped=list(stats['reports']),stats['reports_skipped']
            for entry in pkg['reports']:
                rc=entry['relation_code']
                if rc in skipped:reports_skipped+=1;continue
                if rc not in new_codes:continue
                obj=Report(name=entry['name'],relation_id=rel_map[rc],status='completed',input_snapshot=entry['input_snapshot'],result=entry['result'],created_at=_parse_dt(entry['created_at']))
                db.add(obj);db.flush()
                target=_artifacts()/f'report-{obj.id}.json'
                target.write_bytes(pkg['files'][entry['file']])
                written.append(target)
                reports.append({'id':obj.id,'name':entry['name']})
            if fault=='after_files':raise RuntimeError('注入故障：写文件后落库失败')
            job.stats={'relation_map':rel_map,'relations_skipped':sorted(skipped),'dependencies_added':deps_added,'dependencies_skipped':deps_skipped,'reports':reports,'reports_skipped':reports_skipped}
            job.status,job.error='restored',None
            db.add(Audit(actor='system',action='恢复整册备份 '+checksum[:12]))
            db.commit()
    except Exception as e:
        for f in written:f.unlink(missing_ok=True)
        _mark(job_id,'failed','恢复失败已回滚：'+str(e)[:200],sf)

def requeue_interrupted(session_factory=None):
    """worker启动时重投中断（verifying/restoring）与待处理任务，崩溃后续跑。"""
    sf=session_factory or Session
    with sf() as db:
        stuck=list(db.scalars(select(RestoreJob).where(RestoreJob.status.in_(('verifying','restoring')))))
        queued=list(db.scalars(select(RestoreJob).where(RestoreJob.status=='pending')))
        for job in stuck:job.status='pending'
        db.commit()
        for job in stuck+queued:_enqueue(job.id)

# ---------- 任务管理（路由层调用） ----------

def create_restore_job(db,content,user):
    if not content:raise HTTPException(422,'备份包为空')
    if len(content)>MAX_PACKAGE_BYTES:raise HTTPException(422,'备份包超过20MB上限')
    checksum=_sha256(content)
    existing=db.scalar(select(RestoreJob).where(RestoreJob.package_checksum==checksum))
    if existing:return {**record(existing),'duplicate':True}
    target=_artifacts()/'uploads'/f'{checksum}.swb'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(content)
    job=RestoreJob(package_checksum=checksum,package_path=str(target),status='pending')
    db.add(job);db.add(Audit(actor=user.email,action='上传恢复包 '+checksum[:12]))
    try:db.commit()
    except IntegrityError:
        db.rollback()
        return {**record(db.scalar(select(RestoreJob).where(RestoreJob.package_checksum==checksum))),'duplicate':True}
    try:_enqueue(job.id)
    except Exception:
        job.status,job.error='failed','队列暂时不可用，请重试';db.commit();raise HTTPException(503,job.error)
    return {**record(job),'duplicate':False}

def restores(db):return [record(x) for x in db.scalars(select(RestoreJob).order_by(RestoreJob.id.desc()))]

def retry_restore(db,ident,user):
    job=db.get(RestoreJob,ident)
    if not job:raise HTTPException(404,'恢复任务不存在')
    if job.status=='restored':raise HTTPException(409,'该备份包已恢复完成，无需重试')
    if job.status!='pending':
        job.status,job.error='pending',None
        db.add(Audit(actor=user.email,action='重试恢复包 '+job.package_checksum[:12]));db.commit()
        try:_enqueue(job.id)
        except Exception:raise HTTPException(503,'队列暂时不可用，请重试')
    return record(job)
