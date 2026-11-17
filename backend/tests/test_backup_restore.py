"""整册备份恢复的单元测试（SQLite + 临时文件目录，标准库unittest直接运行）。

覆盖：同一时点快照（备份中改依赖不混入）、进行中任务不混入、目标编号冲突不覆盖、
写文件后落库失败回滚、损坏/缺件/篡改整包拒绝、重试与重复投递幂等、中断续跑。
运行：python3 backend/tests/test_backup_restore.py
"""
import os,sys,io,json,shutil,unittest,hashlib,tarfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from swb_test_env import engine,Session,Base,backup,ART,FAKE
from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Report,RestoreJob,Audit

def read_members(package):
    out={}
    with tarfile.open(fileobj=io.BytesIO(package),mode='r:gz') as tar:
        for m in tar.getmembers():
            if m.isfile():out[m.name]=tar.extractfile(m).read()
    return out

def repack(package,drop=(),modify=None,add=None):
    members=read_members(package)
    for name in drop:members.pop(name,None)
    for name,data in (modify or {}).items():members[name]=data
    for name,data in (add or {}).items():members[name]=data
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for name,data in members.items():
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    return buf.getvalue()

def seed_book():
    """两个草案、4条依赖、1个已完成报告（含原始证据文件）、1个排队与1个执行中任务。"""
    with Session() as db:
        a=Relation(code='NL-1117-A',title='材料检测委托单',attributes=['委托号','客户','检测项','方法','结果'],note='九人小组')
        b=Relation(code='NL-1117-B',title='循环编号与独立属性',attributes=['A','B','C'],note='')
        db.add_all([a,b]);db.flush()
        db.add_all([Dependency(relation_id=a.id,lhs=['委托号'],rhs=['客户'],note=''),
                    Dependency(relation_id=a.id,lhs=['检测项'],rhs=['方法'],note=''),
                    Dependency(relation_id=a.id,lhs=['委托号','检测项'],rhs=['结果'],note=''),
                    Dependency(relation_id=b.id,lhs=['A'],rhs=['B'],note='')])
        db.flush()
        snap={'relation':{'code':'NL-1117-A','attributes':['委托号','客户','检测项','方法','结果']},'dependencies':[{'lhs':['委托号'],'rhs':['客户']}]}
        r1=Report(name='shift-20261117',relation_id=a.id,status='completed',input_snapshot=snap,result={'checksum':'abc123','all_3nf':True},created_at=datetime(2026,11,17,9,20))
        db.add(r1);db.flush()
        original='{"checksum": "abc123", "all_3nf": true, "note": "原始证据"}'.encode()
        Path(ART,f'report-{r1.id}.json').write_bytes(original)
        db.add(Report(name='shift-queued',relation_id=b.id,status='queued',input_snapshot=snap,result=None))
        db.add(Report(name='shift-running',relation_id=b.id,status='running',input_snapshot=snap,result=None))
        db.commit()
        return r1.id,original

class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        shutil.rmtree(ART,ignore_errors=True);os.makedirs(ART)
        FAKE.items.clear()

    def make_job(self,package):
        checksum=hashlib.sha256(package).hexdigest()
        up=Path(ART,'uploads');up.mkdir(parents=True,exist_ok=True)
        path=up/f'{checksum}.swb';path.write_bytes(package)
        with Session() as db:
            job=RestoreJob(package_checksum=checksum,package_path=str(path),status='pending')
            db.add(job);db.commit();return job.id

    def export(self):
        seed_book()
        with Session() as db:return backup.build_backup(db)[0]

    def test_roundtrip(self):
        rid,original=seed_book()
        with Session() as db:package,checksum,counts=backup.build_backup(db)
        self.assertEqual(counts,{'relations':2,'dependencies':4,'reports':1})  # 进行中任务不混入
        backup.verify_package(package)
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        shutil.rmtree(ART);os.makedirs(ART)
        Path(ART,'report-99999.json').write_text('orphan')  # 上次崩溃的孤儿文件
        job_id=self.make_job(package)
        backup.run_restore(job_id)
        with Session() as db:
            job=db.get(RestoreJob,job_id)
            self.assertEqual(job.status,'restored')
            rels={r.code:r for r in db.scalars(select(Relation))}
            self.assertEqual(set(rels),{'NL-1117-A','NL-1117-B'})
            self.assertEqual(rels['NL-1117-A'].attributes,['委托号','客户','检测项','方法','结果'])
            deps=list(db.scalars(select(Dependency)))
            self.assertEqual(len(deps),4)
            self.assertEqual(len([d for d in deps if d.relation_id==rels['NL-1117-A'].id]),3)  # 引用重建到新id
            reps=list(db.scalars(select(Report)))
            self.assertEqual(len(reps),1)
            rep=reps[0]
            self.assertEqual(rep.status,'completed')
            self.assertEqual(rep.relation_id,rels['NL-1117-A'].id)
            self.assertEqual(rep.result,{'checksum':'abc123','all_3nf':True})
            self.assertEqual(rep.input_snapshot['relation']['code'],'NL-1117-A')
            self.assertEqual(rep.created_at.hour,9)
            self.assertEqual(len(list(db.scalars(select(Audit)))),1)
            self.assertFalse(Path(ART,'report-99999.json').exists())  # 孤儿已清理
            self.assertEqual(Path(ART,f'report-{rep.id}.json').read_bytes(),original)  # 原始证据字节保留

    def test_export_snapshot_consistency(self):
        """备份中改依赖：导出事务快照建立后并发提交的修改不混入包内。"""
        seed_book()
        db=Session()
        def hook():
            other=Session()
            b=other.scalar(select(Relation).where(Relation.code=='NL-1117-B'))
            other.add(Dependency(relation_id=b.id,lhs=['B'],rhs=['A'],note='并发修改'))
            other.commit();other.close()
        package,_,counts=backup.build_backup(db,hook=hook)
        db.close()
        self.assertEqual(counts['dependencies'],4)
        self.assertEqual(len(backup.verify_package(package)['dependencies']),4)
        with Session() as check:
            self.assertEqual(len(list(check.scalars(select(Dependency)))),5)

    def test_export_rebuilds_missing_report_file(self):
        rid,original=seed_book()
        Path(ART,f'report-{rid}.json').unlink()
        with Session() as db:package,_,counts=backup.build_backup(db)
        self.assertEqual(counts['reports'],1)
        pkg=backup.verify_package(package)
        self.assertEqual(json.loads(pkg['files'][f'files/report-{rid}.json'])['checksum'],'abc123')

    def test_code_conflict_not_overwritten(self):
        """目标已有同编号草案：不被覆盖，其依赖与报告整组跳过。"""
        package=self.export()
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        with Session() as db:
            local=Relation(code='NL-1117-A',title='本地修改版',attributes=['X','Y'],note='本地')
            db.add(local);db.flush()
            db.add(Dependency(relation_id=local.id,lhs=['X'],rhs=['Y'],note='本地依赖'))
            db.commit()
        job_id=self.make_job(package)
        backup.run_restore(job_id)
        with Session() as db:
            job=db.get(RestoreJob,job_id)
            self.assertEqual(job.status,'restored')
            self.assertEqual(job.stats['relations_skipped'],['NL-1117-A'])
            self.assertEqual(job.stats['reports_skipped'],1)
            self.assertEqual(job.stats['dependencies_skipped'],3)
            local=db.scalar(select(Relation).where(Relation.code=='NL-1117-A'))
            self.assertEqual(local.title,'本地修改版')
            self.assertEqual(local.attributes,['X','Y'])
            self.assertEqual(len(list(db.scalars(select(Dependency).where(Dependency.relation_id==local.id)))),1)
            self.assertTrue(db.scalar(select(Relation).where(Relation.code=='NL-1117-B')))
            self.assertEqual(len(list(db.scalars(select(Report)))),0)

    def test_db_failure_after_files_rolls_back(self):
        """写文件后落库失败：整包回滚，不留半册；重试后完整恢复。"""
        package=self.export()
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        job_id=self.make_job(package)
        backup.run_restore(job_id,fault='after_files')
        with Session() as db:
            job=db.get(RestoreJob,job_id)
            self.assertEqual(job.status,'failed')
            self.assertIn('回滚',job.error)
            self.assertEqual(len(list(db.scalars(select(Relation)))),0)
            self.assertEqual(len(list(db.scalars(select(Dependency)))),0)
            self.assertEqual(len(list(db.scalars(select(Report)))),0)
        self.assertEqual(list(Path(ART).glob('report-*.json')),[])  # 已写文件被清理
        user=SimpleNamespace(email='operator@nanlu.test')
        with Session() as db:backup.retry_restore(db,job_id,user)
        backup.run_restore(job_id)
        with Session() as db:
            self.assertEqual(db.get(RestoreJob,job_id).status,'restored')
            self.assertEqual(len(list(db.scalars(select(Relation)))),2)
            self.assertEqual(len(list(db.scalars(select(Report)))),1)
        self.assertEqual(len(list(Path(ART).glob('report-*.json'))),1)

    def test_retry_and_duplicate_delivery_idempotent(self):
        """重试、并发上传同一包、队列重复投递均不重复新增。"""
        package=self.export()
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        user=SimpleNamespace(email='operator@nanlu.test')
        with Session() as db:r1=backup.create_restore_job(db,package,user)
        with Session() as db:r2=backup.create_restore_job(db,package,user)
        self.assertEqual(r1['id'],r2['id'])
        self.assertFalse(r1['duplicate']);self.assertTrue(r2['duplicate'])
        with Session() as db:self.assertEqual(len(list(db.scalars(select(RestoreJob)))),1)
        backup.run_restore(r1['id'])
        with Session() as db:  # 模拟队列重复投递同一任务
            job=db.get(RestoreJob,r1['id']);job.status='pending';db.commit()
        backup.run_restore(r1['id'])
        with Session() as db:
            self.assertEqual(db.get(RestoreJob,r1['id']).status,'restored')
            self.assertEqual(len(list(db.scalars(select(Relation)))),2)
            self.assertEqual(len(list(db.scalars(select(Dependency)))),4)
            self.assertEqual(len(list(db.scalars(select(Report)))),1)
            with self.assertRaises(HTTPException) as ctx:backup.retry_restore(db,r1['id'],user)
            self.assertEqual(ctx.exception.status_code,409)

    def test_interrupted_job_resumes(self):
        """恢复中断（崩溃在restoring）后worker重启重投，续跑完成。"""
        package=self.export()
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        job_id=self.make_job(package)
        with Session() as db:
            job=db.get(RestoreJob,job_id);job.status='restoring';db.commit()
        FAKE.items.clear()
        backup.requeue_interrupted(Session)
        with Session() as db:self.assertEqual(db.get(RestoreJob,job_id).status,'pending')
        self.assertIn((backup.RESTORE_QUEUE,str(job_id)),FAKE.items)
        backup.run_restore(job_id)
        with Session() as db:
            self.assertEqual(db.get(RestoreJob,job_id).status,'restored')
            self.assertEqual(len(list(db.scalars(select(Relation)))),2)

    def test_corrupt_package_rejected(self):
        with self.assertRaises(backup.PackageError):backup.verify_package(b'not-a-gzip')
        with self.assertRaises(backup.PackageError):backup.verify_package(b'')
        job_id=self.make_job(b'not-a-gzip')
        backup.run_restore(job_id)
        with Session() as db:
            job=db.get(RestoreJob,job_id)
            self.assertEqual(job.status,'failed')
            self.assertIn('损坏',job.error)
            self.assertEqual(len(list(db.scalars(select(Relation)))),0)

    def test_missing_member_rejected(self):
        package=self.export()
        fname=[n for n in read_members(package) if n.startswith('files/')][0]
        with self.assertRaises(backup.PackageError) as ctx:backup.verify_package(repack(package,drop=[fname]))
        self.assertIn('缺件',str(ctx.exception))
        with self.assertRaises(backup.PackageError):backup.verify_package(repack(package,drop=['manifest.json']))

    def test_empty_workbook_roundtrip(self):
        """空册导出与恢复：零草案零报告也是合法包。"""
        with Session() as db:package,_,counts=backup.build_backup(db)
        self.assertEqual(counts,{'relations':0,'dependencies':0,'reports':0})
        backup.verify_package(package)
        job_id=self.make_job(package)
        backup.run_restore(job_id)
        with Session() as db:
            job=db.get(RestoreJob,job_id)
            self.assertEqual(job.status,'restored')
            self.assertEqual(job.stats['relation_map'],{})

    def test_tampered_package_rejected(self):
        package=self.export()
        members=read_members(package)
        reports=json.loads(members['reports.json']);reports[0]['result']['checksum']='hacked'
        with self.assertRaises(backup.PackageError):backup.verify_package(repack(package,modify={'reports.json':json.dumps(reports,ensure_ascii=False).encode()}))
        manifest=json.loads(members['manifest.json']);manifest['counts']['reports']=99
        with self.assertRaises(backup.PackageError) as ctx:backup.verify_package(repack(package,modify={'manifest.json':json.dumps(manifest,ensure_ascii=False).encode()}))
        self.assertIn('签名',str(ctx.exception))
        with self.assertRaises(backup.PackageError):backup.verify_package(repack(package,add={'junk.txt':b'x'}))

if __name__=='__main__':
    unittest.main(verbosity=2)
