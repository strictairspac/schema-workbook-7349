import traceback,json
from pathlib import Path
from src.config.database import Session
from src.config.settings import ARTIFACTS_DIR
from src.models.entities import Report
from src.services.reports import queue
from src.services.normalization import analyze
from src.services.backup import run_restore,requeue_interrupted
try:requeue_interrupted()
except Exception:traceback.print_exc()
while True:
    Path('/tmp/worker-heartbeat').touch()
    item=queue.blpop(['schemaworkbook:queue','schemaworkbook:restore-queue'],timeout=2)
    if not item:continue
    if item[0]=='schemaworkbook:restore-queue':
        try:run_restore(int(item[1]))
        except Exception:traceback.print_exc()
        continue
    ident=int(item[1])
    try:
        with Session() as db:
            obj=db.get(Report,ident);obj.status='running';db.commit();snapshot=obj.input_snapshot
        result=analyze(snapshot['relation']['attributes'],snapshot['dependencies'])
        with Session() as db:
            obj=db.get(Report,ident);obj.result=result;obj.status='completed';db.commit()
        Path(ARTIFACTS_DIR,f'report-{ident}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as e:
        traceback.print_exc()
        with Session() as db:
            obj=db.get(Report,ident)
            if obj:obj.status='failed';obj.error=str(e);db.commit()
