"""Update cached Word TOC pages using the final rendered PDF's page labels.

Run after PDF export, then export again. Preserves the original TOC/bookmarks.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from lxml.etree import QName
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

HERE = Path(__file__).resolve().parents[1]
stem = '技术报告_特色功能完善版'
word = HERE / (stem + '.docx')
pdf = HERE / (stem + '.pdf')
text = subprocess.check_output(['pdftotext','-layout',str(pdf),'-'],text=True)
pages = [p for p in text.split('\f') if p.strip()]
normalize = lambda s: re.sub(r'\s+','',s)
document = Document(word)
entries = []
for sdt in document._element.body:
    if QName(sdt).localname != 'sdt':continue
    for element in sdt.iter():
        if QName(element).localname == 'instrText' and element.text:
            element.text=element.text.replace('\\\\','\\')
        if QName(element).localname != 'p':continue
        runs=[x for x in element.iter() if QName(x).localname=='t']
        if len(runs)<2 or not runs[-1].text or not runs[-1].text.isdigit():continue
        title=runs[0].text
        matched=[]
        for physical,page in enumerate(pages,1):
            lines=[v.strip() for v in page.splitlines() if v.strip()]
            if any(normalize(v)==normalize(title) for v in lines):
                assert lines[-1].isdigit(), (title,lines[-1])
                matched.append((physical,lines[-1]))
        assert len(matched)==1,(title,matched)
        physical,label=matched[0]
        entries.append({'heading':title,'physical_page':physical,'printed_page':label,'previous':runs[-1].text})
        runs[-1].text=label
assert len(entries)==8,entries
settings=document.settings.element
for old in settings.findall(qn('w:updateFields')):settings.remove(old)
update=OxmlElement('w:updateFields');update.set(qn('w:val'),'true');settings.append(update)
document.save(word)
(HERE/'evidence/toc_pages.json').write_text(json.dumps({'pdf_pages':len(pages),'entries':entries},ensure_ascii=False,indent=2)+'\n')
log=HERE/'evidence/revision_log.json';record=json.loads(log.read_text())
record['output_sha256']=hashlib.sha256(word.read_bytes()).hexdigest()
record['toc_updated']=True
log.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'pages':len(pages),'entries':entries},ensure_ascii=False))
