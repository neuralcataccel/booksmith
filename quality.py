"""Bounded local sample QA through the existing queue; publication stays independent."""
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import reading_plan
from extract import EXTRACTOR_VERSION
import repair
import retention
import store
from settings import CONFIG, ROOT, RUNTIME, WORK, contained

VERSION = 2
MAX_SAMPLES = 12


def parent(jid):
    if not re.fullmatch(r'[0-9a-f]{16}',jid):raise ValueError('Invalid conversion identifier.')
    job=store.get_job(jid)
    if not job or job['kind']!='book' or job['status'] not in {'ready','review'}:
        raise ValueError('Checks are available for completed audio or a repair waiting for review.')
    plan=reading_plan.validate(json.loads((WORK/jid/'reading-plan.json').read_text(encoding='utf-8')))
    if job['status']=='review':
        _,manifest=repair.review(WORK/jid,verify_audio=False)
        output=WORK/jid/f"audio-v{plan['extractor']}"/'chapters'
    else:
        output=Path(job['output_path'])
        manifest=json.loads((output/'booksmith.json').read_text(encoding='utf-8'))
        if output.resolve()!=contained(CONFIG['output_root'],manifest['relative_folder']):raise ValueError('Invalid media folder.')
    if (manifest.get('job_id')!=jid or manifest.get('edition_key')!=job['edition_key']
        or manifest.get('reading_plan',{}).get('sha256')!=plan['sha256']
        or manifest.get('source_sha256')!=plan['source_sha256']
        or len(manifest.get('tracks',[]))!=len(plan['chapters'])
        or len(manifest.get('render_index',{}).get('chapters',[]))!=len(plan['chapters'])):
        raise ValueError('The audio edition or retained reading plan changed.')
    fingerprint=reading_plan.digest([VERSION,jid,job['status'],plan['sha256'],manifest])
    return job,plan,manifest,output,fingerprint


def request(jid):
    job,plan,_,_,fingerprint=parent(jid)
    with store.db() as con:existing=con.execute('SELECT * FROM quality_records WHERE parent_id=?',(jid,)).fetchone()
    if existing and existing['fingerprint']==fingerprint and existing['state'] in {'queued','running','complete'}:
        task=store.get_job(existing['task_id'],public=True)
        if task and task['status'] not in {'failed','cancelled'}:return task
    options={'_quality':{'parent':jid,'fingerprint':fingerprint,'version':VERSION,'max_samples':MAX_SAMPLES}}
    task=store.create_job('quality',{'id':job['book_id'],'title':job['title'],'author':job['author']},options,
                          reading_plan.digest(['quality',jid,fingerprint]))
    with store.db() as con:
        con.execute('INSERT OR REPLACE INTO quality_records VALUES (?,?,?,?,NULL,?)',
                    (jid,fingerprint,task['id'],'queued',time.time()))
    return task


def schedule_ready():
    since=float(store.meta('auto_quality_since','0'))
    with store.db() as con:
        jobs=[r[0] for r in con.execute("""SELECT j.id FROM jobs j LEFT JOIN quality_records q ON q.parent_id=j.id
            WHERE j.kind='book' AND j.status IN ('ready','review') AND j.created>=? AND q.parent_id IS NULL
            ORDER BY j.created LIMIT 10""",(since,))]
    for jid in jobs:
        try:request(jid)
        except (ValueError,OSError):continue  # Admission/media failures never alter a ready edition.


def density_flags(words,duration):
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0:
        return ['invalid_duration']
    if words>=12 and duration<words/7.5:return ['unusually_short_audio']
    if words>=12 and duration>words/.5+5:return ['unusually_long_audio']
    return []


def summarize(jid,plan,manifest,audit,asr):
    from qa_novel import words
    total=sum(len(s['pieces']) for c in plan['chapters'] for s in c['spans'])
    samples=[];corrections=[];duration_checks=[]
    for sample in asr:
        ci=sample['chapter']-1;chunk=sample['chunk']
        timing=manifest.get('render_index',{}).get('chapters',[])[ci]['chunks'][chunk]
        reasons=list(sample.get('listening_reasons',[]))
        if sample['needs_listening'] and not reasons:reasons=['low_transcript_agreement']
        reasons+=density_flags(sample['reference_words'],timing['duration'])
        samples.append({'chapter':ci,'chunk':chunk,'title':sample['title'],'start_seconds':timing['start_seconds'],
                        'duration':timing['duration'],'text':sample.get('source_text',sample['text']),
                        'expected_spoken_form':sample['text'],'transcript':sample['transcript'],
                        'sequence_similarity':sample['sequence_similarity'],'needs_listening':bool(reasons),
                        'audio_screen':sample.get('audio_screen',{'checked':False}),
                        'reasons':list(dict.fromkeys(reasons))})
    for ci,(chapter,index) in enumerate(zip(plan['chapters'],manifest['render_index']['chapters'])):
        pieces=[p for s in chapter['spans'] for p in s['pieces']]
        if len(pieces)!=len(index['chunks']):raise ValueError('Quality timing coverage is incomplete.')
        for ti,(piece,timing) in enumerate(zip(pieces,index['chunks'])):
            reasons=density_flags(len(words(piece['text'])),timing['duration'])
            if reasons:duration_checks.append({'chapter':ci,'chunk':ti,'title':chapter['title'],'start_seconds':timing['start_seconds'],'reasons':reasons,'text':piece['text']})
            for outcome in timing.get('pronunciations',[]):
                corrections.append({**outcome,'chapter':ci,'chunk':ti,'title':chapter['title'],'start_seconds':timing['start_seconds'],'text':piece['text']})
    checks={key:audit.get(key) for key in ('source_unchanged','source_plan_structure_matches','all_final_checksums_match',
        'track_titles_match','all_tracks_indexed','plex_track_order_correct','plex_track_titles_correct','range_delivery_passed')}
    flagged={(s['chapter'],s['chunk']) for s in samples if s['needs_listening']}
    flagged.update((s['chapter'],s['chunk']) for s in duration_checks)
    flagged.update((s['chapter'],s['chunk']) for s in corrections if s['status'] not in {'applied','already_correct','dictionary_override'})
    return {'schema':VERSION,'state':'complete','job_id':jid,'checked_passages':len(samples),'total_passages':total,
            'flagged_passages':len(flagged),'samples':samples,'audio_screen_checked':sum(s['audio_screen'].get('checked',False) for s in samples),
            'duration_checks':duration_checks,'corrections':corrections,'checks':checks,
            'publication_pending':audit.get('publication_pending',False),'finished':time.time(),
            'limitations':['Only the listed passages were transcribed; other passages are unchecked by ASR.',
                           'Transcription agreement cannot certify pronunciation, acting or every spoken word.',
                           'Listening positions are approximate MP3 offsets.']}


def stop_tree(process):
    if process.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=20,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def process(job,check_cancel,service_preview=None):
    spec=json.loads(job['options'])['_quality'];jid=spec['parent']
    parent_job,plan,manifest,_,fingerprint=parent(jid)
    if fingerprint!=spec['fingerprint']:raise ValueError('The audio changed after these checks were requested.')
    with store.db() as con:
        current=con.execute('UPDATE quality_records SET state=?,updated=? WHERE parent_id=? AND task_id=?',
                            ('running',time.time(),jid,job['id'])).rowcount
    if current!=1:raise ValueError('A newer quality check superseded this request.')
    qa_python=Path(CONFIG.get('qa_python',RUNTIME/'qa-env'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')))
    if not qa_python.is_file():raise ValueError('Local transcription checks are unavailable; this audio is unchecked.')
    root=WORK/job['id'];root.mkdir(parents=True,exist_ok=True)
    command=[sys.executable,str(ROOT/'qa_novel.py'),jid,'--asr-python',str(qa_python),
             '--timeout-hours','.25','--max-samples',str(spec['max_samples'])]
    store.update_job(job['id'],message='Checking local transcription samples')
    with (root/'quality.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt')
        deadline=time.monotonic()+1200
        try:
            while child.poll() is None:
                check_cancel(job['id'])
                current_parent=store.get_job(jid)
                if not current_parent or current_parent['status']!=parent_job['status']:
                    raise ValueError('This quality check was superseded by an edition-state change.')
                if time.monotonic()>deadline:raise ValueError('Local quality checks timed out; this audio is unchecked.')
                if service_preview:service_preview()
                time.sleep(.5)
        finally:
            stop_tree(child)
    if child.returncode:raise ValueError('Local quality checks did not complete. The audiobook is preserved; inspect or retry the checks.')
    audit_root=RUNTIME/'data'/'qa'/jid
    audit=json.loads((audit_root/'report.json').read_text(encoding='utf-8'))
    asr=json.loads((audit_root/'asr.json').read_text(encoding='utf-8'))
    if audit['state'] not in {'complete','ready_for_listening'} or len(asr)!=audit['samples_planned']:
        raise ValueError('The sample checks are incomplete; unchecked passages are not a pass.')
    if parent(jid)[-1]!=fingerprint:raise ValueError('The edition changed while checking it. Request fresh checks.')
    report=summarize(jid,plan,manifest,audit,asr)
    with store.db() as con:
        con.execute("UPDATE quality_records SET state='complete',report=?,updated=? WHERE parent_id=? AND task_id=?",
                    (json.dumps(report,ensure_ascii=False),time.time(),jid,job['id']))
    store.update_job(job['id'],status='ready',message='Sampled quality checks are complete.')


def failure(job,state,message):
    if job['kind']!='quality':return
    jid=json.loads(job['options'])['_quality']['parent']
    with store.db() as con:
        con.execute('UPDATE quality_records SET state=?,report=?,updated=? WHERE parent_id=? AND task_id=?',
                    (state,json.dumps({'message':message,'state':state}),time.time(),jid,job['id']))


def summary(jid):
    job,plan,manifest,_,fingerprint=parent(jid)
    with store.db() as con:row=con.execute('SELECT * FROM quality_records WHERE parent_id=?',(jid,)).fetchone()
    total=sum(len(s['pieces']) for c in plan['chapters'] for s in c['spans'])
    can_repair=job['status']=='ready' and plan['extractor']==EXTRACTOR_VERSION
    if not row or row['fingerprint']!=fingerprint:return {'state':'not_checked','total_passages':total,'checked_passages':0,'job_id':jid,'title':job['title'],'can_repair':can_repair}
    report=json.loads(row['report']) if row['report'] else {}
    return {**report,'state':row['state'],'task_id':row['task_id'],'job_id':jid,'title':job['title'],
            'total_passages':total,'checked_passages':report.get('checked_passages',0),'can_repair':can_repair}


def audio_path(jid,chapter):
    _,plan,manifest,output,_=parent(jid)
    if not 0<=chapter<len(manifest['tracks']):raise ValueError('Invalid chapter.')
    track=manifest['tracks'][chapter];name=track['file']
    if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('Invalid chapter file.')
    path=contained(output,name)
    if retention.digest(path)!=track['sha256']:raise ValueError('The chapter audio changed. Request a fresh check.')
    return path
