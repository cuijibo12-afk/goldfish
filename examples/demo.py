"""Run locally: PYTHONPATH=. python examples/demo.py"""
import tempfile
from datetime import date
from pathlib import Path
from goldfish.engine import MemoryDB

with tempfile.TemporaryDirectory() as tmp:
    db=MemoryDB(Path(tmp)/'demo.sqlite3')
    raw1=db.add_message('career','user','我以前在建筑工地工作，后来离开。','2025-09-05T00:00:00+00:00')
    raw2=db.add_message('career','user','我开始准备公务员考试。','2026-10-05T00:00:00+00:00')
    raw3=db.add_message('career','user','我面对未知的时候会担心。','2026-10-12T00:00:00+00:00')
    s1=db.add_summary('曾经在建筑工地工作，后来离开',[raw1],'2025-09-05')
    s2=db.add_summary('后来决定准备公务员考试',[raw2],'2026-10-05')
    s3=db.add_summary('曾表达过面对未知时会有担忧',[raw3],'2026-10-12')
    tid=db.add_theme('职业选择','不同阶段的职业决定与相关感受')
    db.attach_theme(tid,s1,'工作经历');db.attach_theme(tid,s2,'考试准备')
    db.add_link(s1,s3,'contextual','职业经历与表达过的未知顾虑处于类似情境')
    print('=== 第一次召回 ===')
    first=db.recall('我当初在工地工作时是怎么想的',max_chars=2100)
    print(first['injection'])
    print('\n=== 第二次（旧记忆仍在当前上下文，不重复）===')
    print(db.recall('工地的话题',context=first['injection'],max_chars=2100)['injection'])
    db.close()
