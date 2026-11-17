"""备份恢复API端到端测试：登录、导出下载、上传恢复、状态机推进、权限控制。
运行：python3 backend/tests/test_backup_api.py （需要httpx）
"""
import os,sys,json,shutil,unittest
from pathlib import Path

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from swb_test_env import engine,Session,Base,backup,ART,FAKE
from sqlalchemy import select
from fastapi.testclient import TestClient
from src.main import app
from src.models.entities import User,Relation,Dependency,Report,RestoreJob,Audit
from src.utils.security import password_hash

def reset_business():
    Base.metadata.create_all(engine)
    with Session() as db:
        for cls in (Relation,Dependency,Report,RestoreJob,Audit):
            db.query(cls).delete()
        db.commit()
    shutil.rmtree(ART,ignore_errors=True);os.makedirs(ART)
    FAKE.items.clear()

def login(client,email):
    res=client.post('/auth/login',json={'email':email,'password':'Demo2026!'})
    assert res.status_code==200,res.text
    return {'Authorization':'Bearer '+res.json()['token']}

class BackupApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        with Session() as db:
            for role in ('admin','operator','viewer'):
                if not db.scalar(select(User).where(User.email==role+'@nanlu.test')):
                    db.add(User(email=role+'@nanlu.test',password=password_hash('Demo2026!'),role=role))
            db.commit()

    def setUp(self):reset_business()

    def seed_via_api(self,client,headers):
        res=client.post('/relations',json={'code':'NL-2001','title':'跨界委托单','attributes':['委托号','客户','检测项'],'note':''},headers=headers)
        self.assertEqual(res.status_code,201,res.text)
        rid=res.json()['id']
        res=client.post('/dependencies',json={'relation_id':rid,'lhs':['委托号'],'rhs':['客户'],'note':''},headers=headers)
        self.assertEqual(res.status_code,201,res.text)
        with Session() as db:  # worker依赖Redis，本地直接落库一个已完成报告与证据文件
            rep=Report(name='shift-x',relation_id=rid,status='completed',input_snapshot={'relation':{'code':'NL-2001'},'dependencies':[]},result={'checksum':'ff','all_3nf':True})
            db.add(rep);db.commit();db.refresh(rep)
            Path(ART,f'report-{rep.id}.json').write_text('{"checksum": "ff", "all_3nf": true}')
            return rep.id

    def test_full_backup_restore_flow(self):
        with TestClient(app) as client:
            headers=login(client,'operator@nanlu.test')
            report_id=self.seed_via_api(client,headers)
            res=client.get('/backups/export',headers=headers)
            self.assertEqual(res.status_code,200)
            package=res.content
            self.assertGreater(len(package),100)
            counts=backup.verify_package(package)
            self.assertEqual(len(counts['reports']),1)
            with Session() as db:  # 导出审计留痕
                self.assertTrue(any('导出整册备份' in a.action for a in db.scalars(select(Audit))))
            reset_business()  # 模拟目标部署：空库
            res=client.post('/backups/restore',content=package,headers=headers)
            self.assertEqual(res.status_code,201,res.text)
            job=res.json()
            self.assertEqual(job['status'],'pending')  # 待校验
            self.assertFalse(job['duplicate'])
            res=client.post('/backups/restore',content=package,headers=headers)  # 并发重复上传
            self.assertTrue(res.json()['duplicate'])
            self.assertEqual(res.json()['id'],job['id'])
            res=client.get('/backups/restores',headers=headers)
            self.assertEqual(len(res.json()),1)
            backup.run_restore(job['id'])  # 模拟worker执行
            res=client.get('/backups/restores',headers=headers)
            self.assertEqual(res.json()[0]['status'],'restored')  # 已恢复
            stats=res.json()[0]['stats']
            self.assertEqual(len(stats['relation_map']),1)
            res=client.get('/relations',headers=headers)
            self.assertEqual([r['code'] for r in res.json()],['NL-2001'])
            res=client.get('/reports',headers=headers)
            self.assertEqual(len(res.json()),1)
            self.assertEqual(res.json()[0]['status'],'completed')
            self.assertEqual(res.json()[0]['result']['checksum'],'ff')
            new_report_id=res.json()[0]['id']
            self.assertTrue(Path(ART,f'report-{new_report_id}.json').exists())  # 证据文件就绪
            with Session() as db:  # 恢复审计留痕
                actions=[a.action for a in db.scalars(select(Audit))]
            self.assertTrue(any('上传恢复包' in a for a in actions))
            self.assertTrue(any('恢复整册备份' in a for a in actions))

    def test_viewer_readonly(self):
        with TestClient(app) as client:
            op=login(client,'operator@nanlu.test')
            self.seed_via_api(client,op)
            viewer=login(client,'viewer@nanlu.test')
            res=client.get('/backups/export',headers=viewer)  # 观察员可导出
            self.assertEqual(res.status_code,200)
            package=res.content
            res=client.post('/backups/restore',content=package,headers=viewer)  # 但不可恢复
            self.assertEqual(res.status_code,403)
            res=client.get('/backups/restores',headers=viewer)  # 可查看任务
            self.assertEqual(res.status_code,200)

    def test_restore_rejects_garbage_via_api(self):
        with TestClient(app) as client:
            headers=login(client,'operator@nanlu.test')
            res=client.post('/backups/restore',content=b'garbage-bytes',headers=headers)
            self.assertEqual(res.status_code,201)
            job_id=res.json()['id']
            backup.run_restore(job_id)
            res=client.get('/backups/restores',headers=headers)
            self.assertEqual(res.json()[0]['status'],'failed')
            self.assertIn('损坏',res.json()[0]['error'])
            res=client.get('/relations',headers=headers)  # 整包拒绝，库中无残留
            self.assertEqual(res.json(),[])

if __name__=='__main__':
    unittest.main(verbosity=2)
