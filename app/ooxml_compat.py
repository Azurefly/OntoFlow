"""Read-only, in-memory compatibility projection for Strict OOXML DOCX.

Transforms namespace identities and relationship Types, never body text or
external references. No ZIP members are extracted to disk; the source is not
modified. Unrecognized extension namespaces remain intact for warnings.
"""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree

NAMESPACES={
 'http://purl.oclc.org/ooxml/wordprocessingml/main':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
 'http://purl.oclc.org/ooxml/officeDocument/relationships':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
 'http://purl.oclc.org/ooxml/drawingml/main':'http://schemas.openxmlformats.org/drawingml/2006/main',
 'http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing':'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
 'http://purl.oclc.org/ooxml/drawingml/picture':'http://schemas.openxmlformats.org/drawingml/2006/picture',
 'http://purl.oclc.org/ooxml/package/relationships':'http://schemas.openxmlformats.org/package/2006/relationships',
}


def _qname(name):
    if isinstance(name,str) and name.startswith('{'):
        ns,local=name[1:].split('}',1)
        return '{'+NAMESPACES.get(ns,ns)+'}'+local
    return name


def compatible_docx_stream(path):
    """Return (readable stream/path, strict_projected). Call after ZIP guards."""
    with ZipFile(path) as archive:
        document=archive.read('word/document.xml')
        if b'http://purl.oclc.org/ooxml/wordprocessingml/main' not in document:
            return str(path),False
        output=BytesIO()
        with ZipFile(output,'w',ZIP_DEFLATED) as target:
            for entry in archive.infolist():
                raw=archive.read(entry.filename)
                if entry.filename.endswith(('.xml','.rels')):
                    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                        raise ValueError('Strict DOCX XML 含 DTD/实体，拒绝自动转换')
                    tree=etree.fromstring(raw,parser=etree.XMLParser(resolve_entities=False,no_network=True))
                    for node in tree.iter():
                        node.tag=_qname(node.tag)
                        for name,value in list(node.attrib.items()):
                            new_name=_qname(name)
                            if name!=new_name:del node.attrib[name]
                            # Only relationship Type URI is a structural value.
                            if name=='Type' and str(node.tag).endswith('}Relationship'):
                                for old,new in NAMESPACES.items():
                                    if value.startswith(old+'/'):value=new+value[len(old):];break
                            node.set(new_name,value)
                    raw=etree.tostring(tree,encoding='UTF-8',xml_declaration=True)
                target.writestr(entry,raw)
        output.seek(0)
        return output,True
