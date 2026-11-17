import {api,upload,downloadFile} from './client.js';
export const backupsApi={restores:()=>api('/backups/restores'),restore:file=>upload('/backups/restore',file),retry:id=>api('/backups/restores/'+id+'/retry','POST'),export:()=>downloadFile('/backups/export','workbook-backup-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'')+'.swb')};
