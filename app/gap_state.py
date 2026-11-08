"""Stable, reviewable extraction issues with append-only human decisions."""
from __future__ import annotations
import hashlib
import json
from .store import j,now

def record_gap(conn,pid,stage,batch,kind,payload,evidence,reason,*,piece=None,cache_key=None):
    piece=piece or {}
    # Explicit targeted retry follows the same issue across a cache-policy
    # version change. Reuse ONLY the authorized issue with unchanged source,
    # category, payload and reason; distinct questions retain separate IDs.
    retry_id=piece.get('_retry_gap_id')
    if retry_id:
        prior=conn.execute('SELECT * FROM extraction_gaps WHERE id=? AND project_id=?',
                           (retry_id,pid)).fetchone()
        if (prior and prior['status']=='retrying' and prior['stage']==stage
            and prior['document_id']==piece.get('document_id')
            and prior['content_hash']==piece.get('content_hash')
            and prior['segment_ordinal']==piece.get('ordinal')
            and prior['kind']==kind and prior['reason']==str(reason)[:900]
            and json.loads(prior['payload'])==payload):
            conn.execute('UPDATE extraction_gaps SET cache_key=? WHERE id=? AND project_id=?',
                         (cache_key,retry_id,pid))
            return retry_id
    key={'project':pid,'stage':stage,'document':piece.get('document_id'),
         'locator':piece.get('locator'),'hash':piece.get('content_hash'),
         'cache_key':cache_key,'batch':batch,'kind':kind,'payload':payload,'reason':str(reason)}
    ident=hashlib.sha256(json.dumps(key,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]
    conn.execute('''INSERT INTO extraction_gaps
        (id,project_id,stage,batch_number,kind,payload,evidence,reason,created_at,
         document_id,content_hash,cache_key,segment_ordinal)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
        (ident,pid,stage,batch,kind,j(payload),j(evidence),str(reason)[:900],now(),
         piece.get('document_id'),piece.get('content_hash'),cache_key,piece.get('ordinal')))
    # An unchanged source must not reopen a human-resolved/deferred question.
    return ident
