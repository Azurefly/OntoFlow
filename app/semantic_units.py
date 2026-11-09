"""Bounded semantic units with lossless, per-occurrence source coordinates.

Only adjacent prose in the same section/source region is coalesced. Tables,
headings, native schemas and layout boundaries are never swallowed. Original
units stay addressable through exact character ranges in source_spans.
"""
from __future__ import annotations

VERSION = 'adjacent-prose-v142'
MAX_CHARS = 4200
MAX_MEMBERS = 128
PROSE_KINDS = {'prose'}
COALESCIBLE_PARTS = {'html','body','pdf_text','pdf_fallback','slide','email_body'}


def coalesce_prose(units, max_chars=MAX_CHARS, max_members=MAX_MEMBERS):
    """Return semantically useful batches, not one request per HTML span.

    No source text is truncated/deduplicated. Small documents (fewer than eight
    prose units) retain their existing layout; there is no filename/domain rule.
    """
    if sum(u.get('kind') in PROSE_KINDS for u in units) < 8:
        return units
    result=[]; group=[]; size=0; key=None
    def flush():
        nonlocal group,size,key
        if not group:return
        if len(group)==1:
            result.append(group[0])
        else:
            parts=[];spans=[];offset=0
            for u in group:
                t=u['text']
                if parts:offset+=2
                spans.append({'start':offset,'end':offset+len(t),
                    'locator':u['locator'],'source_order':u.get('source_order'),
                    'source_part':u.get('source_part','')})
                parts.append(t);offset+=len(t)
            item=dict(group[0]);item.update(text='\n\n'.join(parts),source_spans=spans,
                locator=f"{group[0]['locator']} … {group[-1]['locator']} / 连续正文 {len(group)} 单元",
                context_before=group[0].get('context_before',''),semantic_unit_version=VERSION)
            result.append(item)
        group=[];size=0;key=None
    for u in units:
        eligible=(u.get('kind') in PROSE_KINDS and u.get('source_part') in COALESCIBLE_PARTS
                  and not u.get('layout_warning')
                  and not u.get('source_spans'))
        this_key=(u.get('section',''),u.get('source_part',''),u.get('parser',''))
        if not eligible:
            flush();result.append(u);continue
        extra=len(u['text'])+(2 if group else 0)
        if group and (this_key!=key or size+extra>max_chars or len(group)>=max_members):
            flush();extra=len(u['text'])
        key=this_key;group.append(u);size+=extra
    flush()
    return result


def quote_locations(piece, quote, limit=400):
    """Map a grounded quotation back to its original source occurrences.

    Multi-paragraph quotes get one evidence item per touched range. Repeated
    text inside the same unit returns all matching occurrences, not a guessed
    one. A bounded excerpt remains backed by its exact original substring.
    """
    content=str(piece.get('content',''));quote=str(quote or '').strip()
    if not quote or quote not in content:return []
    base={'document_id':piece['document_id']}
    spans=piece.get('source_spans') or []
    if not spans:return [{**base,'locator':piece['locator'],'quote':quote[:limit]}]
    found=[];start=0
    while True:
        at=content.find(quote,start)
        if at<0:break
        end=at+len(quote)
        for span in spans:
            lo=max(at,span['start']);hi=min(end,span['end'])
            if hi>lo:
                snippet=content[lo:hi].strip()
                if snippet:found.append({**base,'locator':span['locator']+f" / 来源序 {span.get('source_order','?')}",
                    'quote':snippet[:limit]})
        start=at+max(1,len(quote))
    # No invented association if malformed metadata somehow reaches this layer.
    return found or [{**base,'locator':piece['locator'],'quote':quote[:limit]}]
