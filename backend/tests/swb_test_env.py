"""测试共享环境：SQLite文件库（WAL快照）+ 临时产物目录 + 假队列。
两个测试文件可独立运行（python3 backend/tests/test_xxx.py），也可被unittest发现同进程加载。
"""
import os,sys,tempfile
BACKEND=os.path.join(os.path.dirname(os.path.abspath(__file__)),'..')
sys.path.insert(0,BACKEND)
TMP=tempfile.mkdtemp(prefix='swb-test-')
ART=os.path.join(TMP,'artifacts')
os.environ.setdefault('DATABASE_URL','sqlite:///'+os.path.join(TMP,'test.db'))
os.environ.setdefault('ARTIFACTS_DIR',ART)
os.environ.setdefault('JWT_SECRET','test-secret-key')
os.environ.setdefault('SEED_DEMO','false')

from sqlalchemy import text,event
from src.config.database import engine,Session,Base
from src.services import backup

class FakeQueue:
    def __init__(self):self.items=[]
    def rpush(self,*args):self.items.append(args)
FAKE=FakeQueue()
backup.queue=FAKE

with engine.connect() as c:
    c.execute(text('PRAGMA journal_mode=WAL'));c.commit()
# 与生产MySQL（PyMySQL autocommit=False，首条语句即开REPEATABLE READ事务）对齐：
# 让SQLite在事务开始时显式BEGIN，WAL读事务由此获得一致性快照。
@event.listens_for(engine,'connect')
def _sqlite_autocommit(dbapi_conn,rec):dbapi_conn.isolation_level=None
@event.listens_for(engine,'begin')
def _sqlite_begin(conn):conn.exec_driver_sql('BEGIN')
