from fastapi import APIRouter,Depends,Request
from fastapi.responses import Response
from datetime import datetime
from src.config.database import db_session
from src.utils.security import current,writer
from src.models.entities import Audit
from src.services import backup as service
router=APIRouter()
@router.get('/backups/export')
def export(db=Depends(db_session),user=Depends(current)):
    package,checksum,counts=service.build_backup(db)
    db.add(Audit(actor=user.email,action='导出整册备份 '+checksum[:12]));db.commit()
    name='workbook-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.swb'
    return Response(package,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="{name}"'})
@router.post('/backups/restore',status_code=201)
async def restore(request:Request,db=Depends(db_session),user=Depends(writer)):
    return service.create_restore_job(db,await request.body(),user)
@router.get('/backups/restores')
def restores(db=Depends(db_session),user=Depends(current)):return service.restores(db)
@router.post('/backups/restores/{ident}/retry')
def retry(ident:int,db=Depends(db_session),user=Depends(writer)):return service.retry_restore(db,ident,user)
