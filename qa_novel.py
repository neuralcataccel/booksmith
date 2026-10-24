"""Local, read-only narration audit. No book text or audio leaves this machine.

Run with the app environment and a separate faster-whisper interpreter:
  .venv/Scripts/python.exe qa_novel.py JOB_ID --asr-python PATH_TO_QA_PYTHON
Samples/transcripts/report live under runtime/data/qa/JOB_ID, never in Git.
"""
import argparse
import difflib
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def words(text):
    return re.findall(r"[\w]+(?:'[\w]+)?", text.lower().replace('’', "'"))


def compare_transcript(reference, transcript):
    """Conservative listening flags, including tails hidden by a high aggregate score."""
    expected, actual = words(reference), words(transcript)
    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    score = matcher.ratio()
    # A stray one-word match near the end is weak evidence that the ending was read.
    blocks = [block for block in matcher.get_matching_blocks() if block.size >= 2]
    tail = len(expected) - (blocks[-1].a + blocks[-1].size) if blocks else len(expected)
    reasons = []
    if score < .8:
        reasons.append('low_transcript_agreement')
    if tail >= 5 and tail >= .15 * len(expected):
        reasons.append('possible_missing_ending')
    return {'reference_words': len(expected), 'asr_words': len(actual),
            'sequence_similarity': round(score, 4), 'unmatched_reference_tail_words': tail,
            'needs_listening': bool(reasons), 'listening_reasons': reasons}


def choose_samples(chapters, all_chapters=False):
    chosen = []
    for cidx, chapter in enumerate(chapters, 1):
        if not all_chapters and not re.match(r'^chapter\s', chapter['title'], re.IGNORECASE):
            continue
        dialogue = [(index, text) for index, text in enumerate(chapter['chunks'])
                    if any(mark in text for mark in ['“', '”', '"']) and len(words(text)) >= 18]
        candidates = dialogue or [(index, text) for index, text in enumerate(chapter['chunks']) if len(words(text)) >= 18]
        if not candidates and all_chapters:
            candidates=[(index,text) for index,text in enumerate(chapter['chunks']) if len(words(text))>=6]
        if candidates:
            tidx, text = candidates[len(candidates) // 2]
            chosen.append({'chapter': cidx, 'chunk': tidx, 'title': chapter['title'], 'text': text, 'category': 'dialogue' if dialogue else 'prose'})
    # Add number-rich passages spread across the book without duplicating a sample.
    numeric = [{'chapter': cidx, 'chunk': tidx, 'title': chapter['title'], 'text': text, 'category': 'numbers'}
               for cidx, chapter in enumerate(chapters, 1) for tidx, text in enumerate(chapter['chunks'])
               if re.search(r'\d', text) and len(words(text)) >= 18]
    for fraction in (.1, .5, .9):
        if numeric:
            candidate = numeric[min(len(numeric) - 1, int(len(numeric) * fraction))]
            if not any((x['chapter'], x['chunk']) == (candidate['chapter'], candidate['chunk']) for x in chosen):
                chosen.append(candidate)
    return sorted(chosen, key=lambda x: (x['chapter'], x['chunk']))


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def audio_screen(path):
    """Cheap PCM validity/silence screen, separate from recognition agreement."""
    import array
    import wave
    try:
        with wave.open(str(path),'rb') as audio:
            if audio.getsampwidth()!=2 or audio.getnchannels()!=1:
                return {'checked':False,'flags':[],'reason':'unsupported_pcm_format'}
            values=array.array('h',audio.readframes(audio.getnframes()))
        if sys.byteorder!='little':values.byteswap()
        if not values:return {'checked':True,'flags':['empty_audio']}
        peak=max(abs(x) for x in values)
        rms=(sum(x*x for x in values)/len(values))**.5
        flags=[]
        if peak<4 or rms<3:flags.append('silent_audio')
        if sum(abs(x)>=32760 for x in values)/len(values)>.02:flags.append('possible_clipping')
        return {'checked':True,'flags':flags,'peak':peak/32768,'rms':rms/32768}
    except (OSError,wave.Error):
        return {'checked':False,'flags':[],'reason':'unreadable_pcm'}


def transcribe(root):
    # Optional QA dependency is deliberately isolated from the production service.
    from faster_whisper import WhisperModel
    model = WhisperModel('tiny.en', device='cpu', compute_type='int8', cpu_threads=2, local_files_only=True)
    samples = json.loads((root / 'samples.json').read_text(encoding='utf-8'))
    results = []
    for sample in samples:
        wav = root / sample['audio']
        if not wav.is_file():
            continue
        segments, _ = model.transcribe(str(wav), language='en', beam_size=5, vad_filter=False, condition_on_previous_text=False)
        transcript = ' '.join(segment.text.strip() for segment in segments)
        result={**sample, 'transcript': transcript, **compare_transcript(sample['text'], transcript),
                'audio_screen':audio_screen(wav)}
        result['listening_reasons']+=result['audio_screen']['flags']
        result['needs_listening']=bool(result['listening_reasons'])
        results.append(result)
        write_json(root / 'asr.json', results)
    print(json.dumps({'samples_transcribed': len(results), 'flagged_for_listening': sum(x['needs_listening'] for x in results)}), flush=True)


def verify_final(job, manifest, output):
    """Check server delivery and metadata; this does not simulate a family client."""
    import mutagen
    import requests
    import plex_delivery
    from settings import CONFIG
    server = plex_delivery.connect()
    tracks = server.fetchItem(int(job['plex_key'])).tracks()
    wanted = {plex_delivery.normalized(str(Path(plex_delivery.plex_path(Path(manifest['relative_folder']))) / t['file'])) for t in manifest['tracks']}
    tracks = [t for t in tracks if any(plex_delivery.normalized(p.file) in wanted for m in t.media for p in m.parts)]
    tracks.sort(key=lambda t: t.index)
    ordering = [t.index for t in tracks] == list(range(1, len(manifest['tracks']) + 1))
    titles = [t.title for t in tracks] == [t['title'] for t in manifest['tracks']]
    durations, tags_ok = [], True
    for track in manifest['tracks']:
        audio = mutagen.File(output / track['file'], easy=True)
        durations.append(audio.info.length)
        tags_ok = tags_ok and audio.get('title') == [track['title']] and audio.get('album') == [manifest['album_title']]
        tags_ok = tags_ok and audio.get('tracknumber') == [f"{track['number']}/{len(manifest['tracks'])}"]
    served = []
    for index in sorted({0, len(tracks) // 2, len(tracks) - 1}) if tracks else []:
        track = tracks[index]
        part = next(p for m in track.media for p in m.parts if plex_delivery.normalized(p.file) in wanted)
        expected_file = output / Path(part.file.replace('\\', '/')).name
        info = mutagen.File(expected_file).info
        frame_offset = getattr(info, 'frame_offset', 0)
        start = min(65536, max(0, expected_file.stat().st_size - frame_offset - 1024))
        response = requests.get(CONFIG['plex_url'] + part.key,
                                headers={'X-Plex-Token': server._token, 'Range': f'bytes={start}-{start + 1023}'}, timeout=20)
        with expected_file.open('rb') as stream:
            stream.seek(start)
            expected_bytes = stream.read(1024)
            stream.seek(frame_offset + start)
            payload_bytes = stream.read(1024)
        # Plex serves the MPEG payload without the embedded ID3 header on this server.
        # Accept either representation and compare actual audio bytes beyond the header.
        served.append({'track': track.index, 'status': response.status_code,
                       'bytes_match': response.content in (expected_bytes, payload_bytes),
                       'metadata_stripped': frame_offset > 0 and response.content == payload_bytes,
                       'range_start': start,
                       'range_header_valid': response.headers.get('Content-Range', '').startswith(f'bytes {start}-')})
    preferences = server.query(f"/library/sections/{CONFIG['plex_section']}/prefs")
    offsets = next((setting.get('value') for setting in preferences if setting.get('id') == 'enableTrackOffsets'), None)
    return {'plex_track_order_correct': ordering, 'plex_track_titles_correct': titles,
            'embedded_tags_correct': bool(tags_ok), 'encoded_duration_seconds': sum(durations),
            'duration_difference_seconds': round(abs(sum(durations) - manifest['audio_seconds']), 3),
            'plex_track_offsets_enabled': offsets in {'true', '1'}, 'range_delivery': served,
            'range_delivery_passed': bool(served) and all(x['status'] == 206 and x['bytes_match'] and x['range_header_valid'] for x in served)}


def audit(jid, asr_python, timeout_hours, max_samples=None):
    import catalogue
    import plex_delivery
    import retention
    import store
    from extract import EXTRACTOR_VERSION, extract_book
    from settings import RUNTIME, WORK
    if not re.fullmatch(r'[0-9a-f]{16}', jid):
        raise ValueError('Invalid job identifier.')
    job = store.get_job(jid)
    if not job or job['kind'] != 'book':
        raise ValueError('Choose a book conversion.')
    options = json.loads(job['options'])
    source = catalogue.source_file(job['book_id'])
    version=EXTRACTOR_VERSION
    plan=None
    saved_plan=WORK/jid/'reading-plan.json'
    if saved_plan.is_file():
        import reading_plan
        plan=reading_plan.validate(json.loads(saved_plan.read_text(encoding='utf-8')))
        version=plan['extractor']
    extracted = extract_book(source, options,version=version,include_structure=bool(plan))
    legacy = extract_book(source, options, version='4')
    old_text = [t for c in legacy['chapters'] for t in c['chunks']]
    if 'chapter' in options:
        from worker import select_chapters
        extracted=select_chapters(extracted,options)
    source_plan_matches=True
    if plan:
        baseline=reading_plan.build(extracted,options['source_sha256'],{})
        def contents(p):return [(c['title'],[(s['id'],s['text'],s['kind']) for s in c['spans']]) for c in p['chapters']]
        source_plan_matches=contents(baseline)==contents(plan)
        extracted=reading_plan.to_extracted(plan)
    current_text = [t for c in extracted['chapters'] for t in c['chunks']]
    root = RUNTIME / 'data' / 'qa' / jid
    root.mkdir(parents=True, exist_ok=True)
    samples = choose_samples(extracted['chapters'],all_chapters=True)
    if len(extracted['chapters'])<=2:
        # Short-story auditions need more than one sampled passage.
        for ci,chapter in enumerate(extracted['chapters'],1):
            threshold=18 if any(len(words(t))>=18 for t in chapter['chunks']) else 6
            candidates=[(i,t) for i,t in enumerate(chapter['chunks']) if len(words(t))>=threshold]
            for index,text in candidates[:1]+candidates[-1:]:
                if not any(s['chapter']==ci and s['chunk']==index for s in samples):
                    samples.append({'chapter':ci,'chunk':index,'title':chapter['title'],'text':text,'category':'story-edge'})
    if not samples:
        raise ValueError('No representative samples found for this book.')
    if max_samples is not None and len(samples)>max_samples:
        if max_samples==1:samples=samples[:1]
        else:samples=[samples[round(i*(len(samples)-1)/(max_samples-1))] for i in range(max_samples)]
    for sample in samples:
        import pronunciation
        sample['source_text']=sample['text']
        effective=plan.get('chapter_profiles',{}).get(str(sample['chapter']-1),plan['profile']) if plan else options
        sample['text']=pronunciation.apply(sample['text'],effective.get('pronunciations',[]))
        sample['audio'] = f"{sample['chapter']:03}-{sample['chunk']:05}.wav"
    write_json(root / 'samples.json', samples)
    report = {'job_id': jid, 'book_id': job['book_id'], 'title': job['title'], 'state': 'watching',
              'extraction_version': version, 'source_unchanged': retention.digest(source) == options['source_sha256'],
              'source_plan_structure_matches':source_plan_matches,
              'all_chunks_identical_to_legacy_in_order': ('\n'.join(current_text) in '\n'.join(old_text)) if 'chapter' in options else current_text == old_text,
              'chunks': len(current_text), 'characters': extracted['characters'],
              'legacy_tracks': len(legacy['chapters']), 'planned_titles': [c['title'] for c in extracted['chapters']],
              'samples_planned': len(samples), 'started': time.time()}
    write_json(root / 'report.json', report)
    deadline = time.monotonic() + timeout_hours * 3600
    previous = None
    while time.monotonic() < deadline:
        for sample in samples:
            target = root / sample['audio']
            wav = WORK / jid / f'audio-v{version}' / f"chapter-{sample['chapter']:04}" / f"{sample['chunk']:05}.wav"
            if not target.exists() and wav.is_file():
                # Worker publishes WAVs by rename; copy the completed file before cleanup.
                temporary = target.with_suffix('.tmp.wav')
                shutil.copy2(wav, temporary)
                temporary.replace(target)
        job = store.get_job(jid)
        saved = sum((root / s['audio']).is_file() for s in samples)
        checkpoint = (job['status'], job['done_chunks'] // 100, saved)
        if checkpoint != previous:
            print(json.dumps({'status': job['status'], 'chunks': job['done_chunks'], 'total': job['total_chunks'], 'qa_samples_saved': saved}), flush=True)
            previous = checkpoint
        report.update(job_status=job['status'], chunks_done=job['done_chunks'], samples_saved=saved)
        write_json(root / 'report.json', report)
        if job['status'] in {'ready','review'}:
            if job['status']=='review':
                import repair
                _,manifest=repair.review(WORK/jid)
                output=WORK/jid/f'audio-v{version}'/'chapters'
            else:
                output=Path(job['output_path'])
                manifest=json.loads((output/'booksmith.json').read_text(encoding='utf-8'))
            index=manifest.get('render_index',{})
            for sample in samples:
                target=root/sample['audio']
                if target.exists():continue
                try:
                    chapter_index=index['chapters'][sample['chapter']-1]
                    chunk=chapter_index['chunks'][sample['chunk']]
                    final_track=output/chapter_index['file']
                    # MPEG padding/seek is approximate; comparisons remain sampled QA, not alignment proof.
                    temporary=target.with_suffix('.tmp.wav')
                    command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
                             '-ss',str(chunk['start_seconds']),'-i',str(final_track),'-t',str(chunk['duration']),
                             '-ar','24000','-ac','1',str(temporary)]
                    subprocess.run(command,check=True,capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    temporary.replace(target)
                except (KeyError,IndexError):
                    pass  # Old manifests without a timing index still require captured live WAVs.
            report['samples_saved']=sum((root/s['audio']).is_file() for s in samples)
            break
        if job['status'] in {'failed', 'cancelled'}:
            report.update(state='conversion_needs_attention', error=job['error'])
            write_json(root / 'report.json', report)
            raise RuntimeError('Novel conversion needs attention; see the local QA report.')
        time.sleep(15)
    else:
        report['state'] = 'timed_out'
        write_json(root / 'report.json', report)
        raise TimeoutError('Novel QA exceeded the configured wait.')
    pending_review=job['status']=='review'
    rows = [] if pending_review else plex_delivery.indexed_tracks(plex_delivery.plex_path(Path(manifest['relative_folder'])))
    expected = {plex_delivery.normalized(str(Path(plex_delivery.plex_path(Path(manifest['relative_folder']))) / t['file'])) for t in manifest['tracks']}
    report['all_tracks_indexed'] = None if pending_review else expected.issubset({plex_delivery.normalized(r[2]) for r in rows})
    report['publication_pending']=pending_review
    report['all_final_checksums_match'] = all(retention.digest(output / t['file']) == t['sha256'] for t in manifest['tracks'])
    report.update(audio_seconds=manifest['audio_seconds'], synthesis_seconds=manifest['synthesis_seconds'],
                  final_tracks=len(manifest['tracks']), track_titles_match=[t['title'] for t in manifest['tracks']] == report['planned_titles'])
    if not pending_review:report.update(verify_final(job, manifest, output))
    # Independently reproduce phonemizer warnings on captured passages; never hide them.
    from kokoro_onnx.tokenizer import Tokenizer
    import speech
    tokenizer = Tokenizer()
    warnings = []
    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())
    handler = Capture(level=logging.WARNING)
    logger = logging.getLogger('phonemizer')
    logger.addHandler(handler)
    warning_samples = 0
    try:
        for sample in samples:
            before = len(warnings)
            tokenizer.phonemize(sample['text'], lang=speech.voice_info(options['voice'])['lang'])
            warning_samples += len(warnings) > before
    finally:
        logger.removeHandler(handler)
    report.update(samples_with_phonemizer_warning=warning_samples, warning_messages=warnings)
    result = subprocess.run([asr_python, str(Path(__file__).resolve()), '--transcribe', str(root)],
                            capture_output=True, text=True, timeout=1800,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        report.update(state='asr_needs_attention', asr_error=result.stderr[-1000:])
        write_json(root / 'report.json', report)
        raise RuntimeError('Local ASR check failed; see the QA report.')
    asr = json.loads((root / 'asr.json').read_text(encoding='utf-8'))
    required=['source_unchanged','source_plan_structure_matches','all_final_checksums_match','track_titles_match']
    if not pending_review:required+=['all_tracks_indexed','plex_track_order_correct','plex_track_titles_correct',
                                      'embedded_tags_correct','plex_track_offsets_enabled','range_delivery_passed']
    integrity = all(report[key] for key in required)
    if not plan:integrity=integrity and report['all_chunks_identical_to_legacy_in_order']
    integrity = integrity and (pending_review or report['duration_difference_seconds'] < 2) and len(asr) == len(samples)
    report.update(state=('ready_for_listening' if pending_review else 'complete') if integrity else 'needs_attention', finished=time.time(), samples_transcribed=len(asr),
                  reference_words=sum(x['reference_words'] for x in asr), asr_words=sum(x['asr_words'] for x in asr),
                  mean_sequence_similarity=round(sum(x['sequence_similarity'] for x in asr) / max(1, len(asr)), 4),
                  samples_flagged_for_listening=sum(x['needs_listening'] for x in asr),
                  limitations=['Sampled ASR is a completeness screen, not a listening-quality score.',
                               'Actual family Plex client chapter/resume behavior still needs a human check.'])
    write_json(root / 'report.json', report)
    print(json.dumps({k:report[k] for k in ('state','final_tracks','all_tracks_indexed','all_final_checksums_match','samples_transcribed','mean_sequence_similarity','samples_flagged_for_listening')}), flush=True)
    if not integrity:
        raise RuntimeError('A final integrity check needs attention; see the local QA report.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id', nargs='?')
    parser.add_argument('--asr-python')
    parser.add_argument('--timeout-hours', type=float, default=4)
    parser.add_argument('--transcribe', type=Path)
    parser.add_argument('--max-samples',type=int,choices=range(1,101))
    args = parser.parse_args()
    if args.transcribe:
        transcribe(args.transcribe)
    elif args.job_id and args.asr_python:
        audit(args.job_id, args.asr_python, args.timeout_hours,args.max_samples)
    else:
        parser.error('Supply JOB_ID and --asr-python, or --transcribe SAMPLE_DIRECTORY.')
