import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import soundfile as sf

import catalogue
import annotations
import occurrence_phones
import quality
import plex_delivery
import retention
import reading_plan
import repair
import speech
import store
import native_speech
import context_pauses
from native_speech import NativeUnavailable
from extract import EXTRACTOR_VERSION, extract_book, split_text
from settings import CONFIG, WORK, safe_name

STOP = threading.Event()
WAKE = threading.Event()
THREAD = None
VERIFY_THREAD = None
ACTIVE = None


class Cancelled(Exception):
    pass


def check_cancel(jid):
    if STOP.is_set():
        raise InterruptedError('Service is stopping; this conversion will resume.')
    job = store.get_job(jid)
    if job['cancel_requested']:
        raise Cancelled()


def ffmpeg(args):
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Audio encoding failed: ' + result.stderr[-500:])


def read_duration(path):
    info = sf.info(str(path))
    if info.frames <= 0 or info.samplerate != 24000:
        raise ValueError('Invalid cached narration chunk')
    return info.duration


def preview_parts(chapter,limit=900,include_indexes=False):
    """A contiguous opening passage, omitting structural headings, with block boundaries."""
    entries=[]
    for block in chapter.get('blocks',[]):
        if block['kind'].startswith('h'):continue
        pieces=chapter['chunks'][block['chunk_start']:block['chunk_start']+block['chunk_count']]
        entries.extend((text,i==len(pieces)-1,block['chunk_start']+i) for i,text in enumerate(pieces))
    if not entries:entries=[(text,True,i) for i,text in enumerate(chapter['chunks'])]
    result=[]
    count=0
    for text,end,index in entries:
        if result and count+len(text)>limit:break
        result.append((text,end,index) if include_indexes else (text,end));count+=len(text)
        if count>=600:break
    return result


def process_preview(job):
    options = json.loads(job['options'])
    root = WORK / job['id']
    root.mkdir(exist_ok=True)
    parts = split_text(speech.PREVIEW_TEXT)
    boundaries=[True]*len(parts)
    preview_contexts=None
    if options.get('_occurrence_preview'):
        spec=options['_occurrence_preview']
        if retention.digest(catalogue.source_file(spec['book_id']))!=options['source_sha256']:
            raise ValueError('The source ebook changed. Request a new preview.')
        parts=[spec['text']];boundaries=[True]
        preview_contexts=[[{k:spec[k] for k in ('id','word','start','end','choice')}]] if spec['choice'] is not None else [[]]
    elif options.get('_preview_book'):
        source=catalogue.source_file(options['_preview_book'])
        if retention.digest(source)!=options['source_sha256']:
            raise ValueError('The source ebook changed. Request a new preview.')
        extracted=extract_book(source,options,include_structure=True)
        extracted=select_chapters(extracted,options)
        chapter=extracted['chapters'][0]
        selected=preview_parts(chapter,include_indexes=True)
        parts=[text for text,_,_ in selected]
        boundaries=[end for _,end,_ in selected]
        if options.get('_occurrences'):
            plan=reading_plan.build({**extracted,'version':EXTRACTOR_VERSION},options['source_sha256'],{})
            contexts=[occurrence_phones.chunk_occurrences(plan,span,piece,options['_occurrences'])
                      for span in plan['chapters'][0]['spans'] for piece in span['pieces']]
            preview_contexts=[contexts[index] for _,_,index in selected]
        (root/'preview-source.json').write_text(json.dumps({'book_id':options['_preview_book'],'chapter':options.get('chapter',0),'parts':parts}),encoding='utf-8')
    store.update_job(job['id'], total_chunks=len(parts), total_chapters=1, message='Recording your voice preview')
    duration, spent = 0.0, 0.0
    for i, text in enumerate(parts):
        check_cancel(job['id'])
        p = root / f'{i:04}.wav'
        render_options=dict(options)
        if preview_contexts is not None:
            render_options['_chunk_occurrences']=preview_contexts[i]
            render_options['_context_renderer']=1
        if options.get('pause_mode')=='paragraph':render_options['_pause_seconds']=options['paragraph_pause'] if boundaries[i] or i==len(parts)-1 else 0
        d, s = speech.generate(text, render_options, p)
        duration += d
        spent += s
        store.update_job(job['id'], done_chunks=i+1, audio_seconds=duration, synthesis_seconds=spent)
    concat = root / 'chunks.txt'
    concat.write_text('\n'.join("file '" + (root / f'{i:04}.wav').as_posix().replace("'", "'\\''") + "'" for i in range(len(parts))), encoding='utf-8')
    output = root / 'preview.mp3'
    ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(concat), '-c:a', 'libmp3lame', '-b:a', '96k', str(output)])
    store.update_job(job['id'], status='ready', message='Your preview is ready', done_chapters=1, output_path=str(output))


def service_preview():
    # Lightweight reviews, like voice previews, need not wait for an entire novel.
    preview = store.claim_next(interactive_only=True)
    if preview:
        run_job(preview)


def tag_audio(path, job, options, title, index, total, album_title, cover):
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TCON, COMM, APIC
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=job['author']))
    tags.add(TPE2(encoding=3, text=job['author']))
    tags.add(TALB(encoding=3, text=album_title))
    tags.add(TRCK(encoding=3, text=f'{index}/{total}'))
    tags.add(TCON(encoding=3, text='Audiobook'))
    v = speech.voice_info(options['voice'])['name']
    tags.add(COMM(encoding=3, lang='eng', desc='Narration', text=f'AI narration by {v}; generated locally by Booksmith.'))
    if cover and cover.is_file():
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover.read_bytes()))
    tags.save(str(path), v2_version=3)


def output_names(job, options):
    voice = speech.voice_info(options['voice'])['name']
    style = speech.style_info(options['style'])['name']
    pace = f"{options['pace']:g}x"
    album = f"{job['title']} ({voice}, {style}, {pace})"
    if options.get('_pause_policy') or speech.is_native(options) or options.get('pause_mode')=='paragraph' or options.get('pronunciations') or options.get('_occurrences') or 'chapter' in options or options.get('_repair'):
        # Distinct tuned editions need distinct Plex album tags, not only folders.
        profile='Repair' if options.get('_repair') else options.get('profile',{}).get('name','Edition' if speech.is_native(options) else 'Tuned')
        album += f" · {profile[:40]} {job['edition_key'][:6]}"
    relative = Path(safe_name(job['author'], 70)) / safe_name(album + ' - ' + job['edition_key'][:8], 150)
    return album, relative


def legacy_chunks(root, source, options):
    """Match old cache by exact source text, never by changed chapter positions."""
    found = {}
    for previous, version in [(root / 'audio-v6', '6'), (root / 'audio-v5', '5'), (root, '4')]:
        if not any(previous.glob('chapter-[0-9][0-9][0-9][0-9]')):
            continue
        old = extract_book(source, options, version=version)
        old_timings = previous / 'timings.json'
        timings = json.loads(old_timings.read_text()) if old_timings.exists() else {}
        for cidx, chapter in enumerate(old['chapters'], 1):
            for tidx, text in enumerate(chapter['chunks']):
                path = previous / f'chapter-{cidx:04}' / f'{tidx:05}.wav'
                if path.is_file():
                    found.setdefault(text, (path, timings.get(f'{cidx}/{tidx}', 0)))
    return found


def select_chapters(extracted,options):
    if 'chapter' not in options:return extracted
    index=options['chapter']
    if not isinstance(index,int) or not 0<=index<len(extracted['chapters']):
        raise ValueError('The selected chapter does not exist.')
    chapter=extracted['chapters'][index]
    return {**extracted,'chapters':[chapter],'characters':sum(map(len,chapter['chunks']))}


def verify_staging(stage, tracks, has_cover):
    allowed = {track['file'] for track in tracks} | {'booksmith.json'}
    if has_cover:
        allowed.add('cover.jpg')
    if {path.name for path in stage.iterdir()} != allowed:
        raise RuntimeError('NAS staging contains unexpected or missing files; nothing was published.')
    for track in tracks:
        path = stage / track['file']
        if path.stat().st_size != track['bytes'] or retention.digest(path) != track['sha256']:
            raise RuntimeError('A staged audio copy failed its checksum; local audio is retained for retry.')


def publish(job,manifest,chapters_root,cover):
    check_cancel(job['id'])
    relative=Path(manifest['relative_folder'])
    final=Path(CONFIG['output_root'])/relative
    store.update_job(job['id'],status='delivering',message='Sending completed chapters to the NAS')
    stage=Path(CONFIG['staging_root'])/f"{job['id']}-v{manifest['extraction_version']}"
    stage.mkdir(parents=True,exist_ok=True)
    for track in manifest['tracks']:shutil.copy2(chapters_root/track['file'],stage/track['file'])
    if cover:shutil.copy2(cover,stage/'cover.jpg')
    (stage/'booksmith.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    verify_staging(stage,manifest['tracks'],bool(cover))
    final.parent.mkdir(parents=True,exist_ok=True)
    if final.exists():raise RuntimeError('The output folder already exists without a valid completion record. Nothing was overwritten.')
    check_cancel(job['id']);stage.rename(final)
    store.update_job(job['id'],status='awaiting_plex',output_path=str(final),message='Audio is complete. Waiting for Plex to index it.')
    try:plex_delivery.scan(relative)
    except Exception:logging.exception('Plex scan deferred for job %s',job['id'])


def process_book(job):
    options = json.loads(job['options'])
    root = WORK / job['id']
    root.mkdir(exist_ok=True)
    album_title, relative = output_names(job, options)
    final = Path(CONFIG['output_root']) / relative
    manifest_path = final / 'booksmith.json'
    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text(encoding='utf-8'))
        if prior['edition_key'] != job['edition_key']:
            raise RuntimeError('A different narration already occupies this output folder.')
        store.update_job(job['id'], status='awaiting_plex', output_path=str(final), message='Checking Plex import')
        plex_delivery.scan(relative)
        return
    check_cancel(job['id'])
    store.update_job(job['id'], message='Reading the ebook and finding chapters')
    source = catalogue.source_file(job['book_id'])
    with source.open('rb') as stream:
        source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    if source_hash != options['source_sha256']:
        raise RuntimeError('The source ebook changed after this request. Start a new conversion for the updated edition.')
    version=EXTRACTOR_VERSION
    saved_plan=root/'reading-plan.json'
    if saved_plan.exists():
        prior_plan=reading_plan.validate(json.loads(saved_plan.read_text(encoding='utf-8')))
        version=prior_plan['extractor']
        if version not in {'4','5','6',EXTRACTOR_VERSION}:
            raise ValueError('This job needs an unavailable extraction version.')
    extracted = select_chapters(extract_book(source, options, version=version, include_structure=True),options)
    extracted={**extracted,'version':version}
    repair_parent=None
    if options.get('_repair'):
        extracted,old_plan,parent_manifest,parent_output=repair.prepare(extracted,options)
        repair_parent=(old_plan,parent_manifest,parent_output)
    plan = reading_plan.build(extracted, source_hash, speech.profile_snapshot(options))
    if repair_parent:
        old_plan=repair_parent[0]
        target=options['_repair']['chapter']
        plan['chapter_profiles']=copy_profiles={str(i):old_plan.get('chapter_profiles',{}).get(str(i),old_plan['profile'])
                                              for i in range(len(plan['chapters']))}
        copy_profiles[str(target)]=plan['profile']
        plan['profile']=old_plan['profile']
        plan['repair']=options['_repair']
        plan['sha256']=reading_plan.fingerprint(plan)
    if options.get('_occurrences'):
        occurrence_phones.validate_plan(plan,options['_occurrences'])
    reading_plan.save(root / 'reading-plan.json', plan)
    upgraded_key = speech.cache_key(job['book_id'], options, source_hash,extraction_version=version)
    store.rekey_running_job(job['id'], job['edition_key'], upgraded_key)
    job = {**job, 'edition_key': upgraded_key}
    album_title, relative = output_names(job, options)
    final = Path(CONFIG['output_root']) / relative
    # Keep versioned chunk positions separate from the legacy in-flight plan.
    audio_root = root / f'audio-v{version}'
    audio_root.mkdir(exist_ok=True)
    legacy = {} if options.get('_pause_policy') or speech.is_native(options) or options.get('_repair') or options.get('pause_mode')=='paragraph' or options.get('pronunciations') or options.get('_occurrences') or 'chapter' in options else legacy_chunks(root, source, options)
    chapters = extracted['chapters']
    count = sum(len(c['chunks']) for c in chapters)
    store.update_job(job['id'], total_chunks=count, total_chapters=len(chapters), done_chunks=0, done_chapters=0, audio_seconds=0, message='Narrating chapter 1')
    timing_file = audio_root / 'timings.json'
    timings = json.loads(timing_file.read_text()) if timing_file.exists() else {}
    elapsed = sum(timings.values())
    done, total_audio = 0, 0.0
    tracks = []
    render_index = {'schema': 1, 'plan_sha256': plan['sha256'], 'chapters': []}
    chapters_root = audio_root / 'chapters'
    chapters_root.mkdir(exist_ok=True)
    cover = catalogue.cover_path(job['book_id'])
    approval=store.meta('repair-approved-'+job['id']) if repair_parent else None
    if approval:
        approved_plan,approved_manifest=repair.review(root)
        if json.loads(approval)!={'plan':plan['sha256'],'review':reading_plan.digest(approved_manifest)}:
            raise ValueError('The repair changed after approval. Nothing was published.')
        # Publish the exact reviewed MP3 bytes, without re-encoding cached WAVs.
        store.update_job(job['id'],done_chunks=count,done_chapters=len(chapters),audio_seconds=approved_manifest['audio_seconds'],synthesis_seconds=approved_manifest['synthesis_seconds'])
        publish(job,approved_manifest,chapters_root,cover)
        return
    for cidx, chapter in enumerate(chapters, 1):
        check_cancel(job['id'])
        if repair_parent and cidx-1!=options['_repair']['chapter']:
            parent_track=repair_parent[1]['tracks'][cidx-1]
            original=repair_parent[2]/parent_track['file']
            if retention.digest(original)!=parent_track['sha256']:
                raise ValueError('An unchanged parent track failed its checksum. Original edition retained.')
            audio_hash=repair.payload_digest(original)
            name=parent_track['file'];target=chapters_root/name
            shutil.copy2(original,target)
            tag_audio(target,job,options,chapter['title'],cidx,len(chapters),album_title,cover)
            if repair.payload_digest(target)!=audio_hash:raise ValueError('Retagging changed unchanged-chapter audio.')
            tracks.append({'file':name,'title':chapter['title'],'number':cidx,'sha256':retention.digest(target),
                           'bytes':target.stat().st_size,'copied_audio_sha256':audio_hash})
            chapter_index=json.loads(json.dumps(repair_parent[1]['render_index']['chapters'][cidx-1]))
            chapter_index['file']=name;render_index['chapters'].append(chapter_index)
            temporary_index=root/'render-index.tmp.json'
            temporary_index.write_text(json.dumps(render_index),encoding='utf-8');temporary_index.replace(root/'render-index.json')
            done+=len(chapter['chunks']);total_audio+=sum(p['duration'] for p in chapter_index['chunks'])
            store.update_job(job['id'],done_chunks=done,done_chapters=cidx,audio_seconds=total_audio)
            continue
        chapter_root = audio_root / f'chapter-{cidx:04}'
        chapter_root.mkdir(exist_ok=True)
        wavs = []
        chunk_sources = [(span['id'], pi) for span in plan['chapters'][cidx - 1]['spans']
                         for pi, _ in enumerate(span['pieces'])]
        spans_by_id={span['id']:span for span in plan['chapters'][cidx-1]['spans']}
        chapter_index = {'number': cidx, 'title': chapter['title'], 'chunks': []}
        chapter_seconds = 0.0
        store.update_job(job['id'], message=f"Chapter {cidx} of {len(chapters)} · {chapter['title']}")
        for tidx, text in enumerate(chapter['chunks']):
            check_cancel(job['id'])
            p = chapter_root / f'{tidx:05}.wav'
            span_id,piece_index=chunk_sources[tidx]
            span=spans_by_id[span_id];piece=span['pieces'][piece_index]
            render_options=dict(options)
            if options.get('_occurrences'):
                render_options['_chunk_occurrences']=occurrence_phones.chunk_occurrences(plan,span,piece,options['_occurrences'])
            if options.get('pause_mode')=='paragraph':
                last_piece=piece_index==len(span['pieces'])-1
                render_options['_pause_seconds']=(options['chapter_pause'] if tidx==len(chapter['chunks'])-1 else options['paragraph_pause']) if last_piece else 0
            if not p.exists() and text in legacy:
                previous, seconds = legacy[text]
                try:
                    read_duration(previous)
                    temporary = p.with_suffix('.tmp.wav')
                    shutil.copy2(previous, temporary)
                    temporary.replace(p)
                    timings[f'{cidx}/{tidx}'] = seconds
                    elapsed = sum(timings.values())
                    temp = timing_file.with_suffix('.tmp')
                    temp.write_text(json.dumps(timings))
                    temp.replace(timing_file)
                except (OSError, ValueError, RuntimeError):
                    logging.warning('Legacy chunk could not be reused for %s chapter %s chunk %s', job['id'], cidx, tidx)
            if p.is_file():
                try:
                    duration = read_duration(p)
                    speech.validate_cached(text,render_options,p)
                except Exception:
                    p.unlink()
            if not p.is_file():
                last_error = None
                for attempt in range(3):
                    try:
                        duration, seconds = speech.generate(text, render_options, p)
                        timings[f'{cidx}/{tidx}'] = seconds
                        elapsed = sum(timings.values())
                        temp = timing_file.with_suffix('.tmp')
                        temp.write_text(json.dumps(timings))
                        temp.replace(timing_file)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            STOP.wait(attempt + 1)
                if last_error:
                    raise last_error
            done += 1
            total_audio += duration
            span_id, piece_index = chunk_sources[tidx]
            chapter_index['chunks'].append({'span_id': span_id, 'piece': piece_index,
                                           'start_seconds': chapter_seconds, 'duration': duration})
            if speech.is_native(options):chapter_index['chunks'][-1]['engine']=speech.engine_descriptor(options)
            receipt=speech.context_receipt(text,render_options,p)
            if receipt:
                chapter_index['chunks'][-1]['pronunciations']=receipt['outcomes']
                chapter_index['chunks'][-1]['phone_input_sha256']=receipt['phone_input_sha256']
            chapter_seconds += duration
            wavs.append(p)
            store.update_job(job['id'], done_chunks=done, audio_seconds=total_audio, synthesis_seconds=elapsed)
            service_preview()
        if options.get('_pause_policy'):
            store.update_job(job['id'],message=f"Reviewing sentence and paragraph timing · chapter {cidx}")
            wavs,pause_result=context_pauses.chapter(job,plan,cidx-1,wavs,chapter_index,options,lambda:check_cancel(job['id']))
            total_audio+=pause_result['inserted_samples']/24000
            chapter_seconds=pause_result['audio_seconds']
        concat = chapter_root / 'concat.txt'
        concat.write_text('\n'.join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in wavs), encoding='utf-8')
        name = f'{cidx:03} - {safe_name(chapter["title"], 100)}.mp3'
        mp3 = chapters_root / name
        temporary = chapters_root / f'{cidx:03}.tmp.mp3'
        ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(concat), '-c:a', 'libmp3lame', '-b:a', '96k', '-ar', '24000', '-ac', '1', str(temporary)])
        tag_audio(temporary, job, options, chapter['title'], cidx, len(chapters), album_title, cover)
        temporary.replace(mp3)
        tracks.append({'file': name, 'title': chapter['title'], 'number': cidx,
                       'sha256': retention.digest(mp3), 'bytes': mp3.stat().st_size})
        chapter_index['file'] = name
        render_index['chapters'].append(chapter_index)
        temporary_index = root / 'render-index.tmp.json'
        temporary_index.write_text(json.dumps(render_index), encoding='utf-8')
        temporary_index.replace(root / 'render-index.json')
        store.update_job(job['id'], done_chapters=cidx)
    check_cancel(job['id'])
    book = catalogue.get(job['book_id'], detail=True)
    summary = (book.get('description') or '') + f"\n\nAI narration generated by Booksmith. Voice: {speech.voice_info(options['voice'])['name']}. Pacing: {speech.style_info(options['style'])['name']}."
    manifest = {'schema': 1, 'job_id': job['id'], 'calibre_id': job['book_id'], 'title': job['title'], 'author': job['author'], 'album_title': album_title, 'summary': summary.strip(), 'options': options, 'edition_key': job['edition_key'], 'engine': speech.ENGINE_VERSION, 'model_sha256': CONFIG['model_sha256'], 'source_sha256': source_hash, 'extraction_version': version, 'characters': extracted['characters'], 'tracks': tracks, 'audio_seconds': total_audio, 'synthesis_seconds': elapsed, 'relative_folder': relative.as_posix(), 'created': time.time()}
    if speech.is_native(options):
        engine=speech.engine_descriptor(options)
        manifest.update(engine=engine['runtime'],model_sha256=engine['model'],renderer=engine)
    manifest['reading_plan'] = {'schema': plan['schema'], 'sha256': plan['sha256'], 'profile': plan['profile']}
    manifest['render_index'] = render_index
    if options.get('_pause_policy'):manifest['contextual_pauses']=options['_pause_policy']
    if repair_parent:
        manifest['repair']=options['_repair']
        temporary=root/'review.tmp.json';temporary.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8');temporary.replace(root/'review.json')
        store.update_job(job['id'],status='review',message='Chapter repair is ready to hear. Approve to publish a separate complete edition.')
        return
    publish(job,manifest,chapters_root,cover)


def run_job(job):
    try:
        check_cancel(job['id'])
        if job['kind'] in {'book','preview'} and speech.is_native(json.loads(job['options'])) and not native_speech.available():
            raise NativeUnavailable('Waiting for the GPU renderer and camera headroom; completed audio is retained.')
        if job['kind'] == 'preview':
            process_preview(job)
        elif job['kind']=='analysis':
            annotations.process(job,check_cancel)
        elif job['kind']=='quality':
            quality.process(job,check_cancel,service_preview)
        else:
            process_book(job)
    except NativeUnavailable as exc:
        store.update_job(job['id'],status='queued',message=str(exc),error=None)
        STOP.wait(10)
    except Cancelled:
        quality.failure(job,'cancelled','Local checks were cancelled; the audiobook is preserved.')
        store.update_job(job['id'], status='cancelled', message='Cancelled. Completed chunks are kept for a retry.')
    except InterruptedError:
        quality.failure(job,'queued','Local checks will resume after restart.')
        store.update_job(job['id'], status='queued', message='Paused by service restart; will resume automatically')
    except Exception as exc:
        quality.failure(job,'failed','Local checks are unavailable or incomplete. The audiobook is preserved; retry or listen to it.')
        logging.exception('Job %s failed', job['id'])
        store.update_job(job['id'], status='failed', message='Conversion needs attention', error=str(exc)[:600])


def verify_pending():
    with store.db() as con:
        pending = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE status='awaiting_plex' AND cancel_requested=0")]
    for job in pending:
        try:
            if not plex_delivery.confirm_import(job):
                if time.time() - job['updated'] > 90:
                    manifest = json.loads((Path(job['output_path']) / 'booksmith.json').read_text(encoding='utf-8'))
                    plex_delivery.scan(Path(manifest['relative_folder']))
                    store.update_job(job['id'], message='Audio is complete; Plex is still indexing. Retrying the scan.', error=None)
            else:
                try:quality.request(job['id'])
                except (ValueError,OSError):pass
        except Exception as exc:
            logging.warning('Plex import check deferred for %s: %s', job['id'], type(exc).__name__)
            store.update_job(job['id'], message='Audio is safe on the NAS; waiting for Plex to reconnect.', error='Plex import could not be verified yet.')


def loop():
    global ACTIVE
    last_sync = time.time()
    while not STOP.is_set():
        try:
            job = store.claim_next()
            if job:
                ACTIVE = job['id']
                run_job(job)
                ACTIVE = None
                continue
            if time.time() - last_sync > 3600:
                catalogue.sync()
                last_sync = time.time()
        except Exception:
            logging.exception('Worker iteration interrupted; retrying safely')
            STOP.wait(5)
        WAKE.wait(2)
        WAKE.clear()


def verification_loop():
    last_cleanup = 0
    while not STOP.is_set():
        try:
            verify_pending()
            quality.schedule_ready()
            if time.time() - last_cleanup >= 60:
                retention.sweep()
                last_cleanup = time.time()
        except Exception:
            logging.exception('Plex verification deferred')
        STOP.wait(15)


def start():
    global THREAD, VERIFY_THREAD
    STOP.clear()
    store.recover()
    THREAD = threading.Thread(target=loop, name='booksmith-worker', daemon=True)
    THREAD.start()
    VERIFY_THREAD = threading.Thread(target=verification_loop, name='plex-verifier', daemon=True)
    VERIFY_THREAD.start()


def stop():
    STOP.set()
    WAKE.set()
    if THREAD:
        THREAD.join(timeout=25)
    if VERIFY_THREAD:
        VERIFY_THREAD.join(timeout=15)
