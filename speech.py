import hashlib
import importlib.metadata
import json
import threading
import time

import pronunciation
import occurrence_phones
from reading_plan import digest

from settings import CONFIG, MODELS
from extract import EXTRACTOR_VERSION
from speech_engines import NATIVE_RUNTIME, native_descriptor

DEFAULT_VOICE = 'am_eric'

VOICES = [
    {'id': 'af_heart', 'name': 'Heart', 'description': 'American · feminine · warm', 'lang': 'en-us'},
    {'id': 'af_bella', 'name': 'Bella', 'description': 'American · feminine · bright', 'lang': 'en-us'},
    {'id': 'af_sarah', 'name': 'Sarah', 'description': 'American · feminine · clear', 'lang': 'en-us'},
    {'id': 'af_nicole', 'name': 'Nicole', 'description': 'American · feminine · soft', 'lang': 'en-us'},
    {'id': 'am_michael', 'name': 'Michael', 'description': 'American · masculine', 'lang': 'en-us'},
    {'id': 'am_fenrir', 'name': 'Fenrir', 'description': 'American · masculine', 'lang': 'en-us'},
    {'id': 'am_eric', 'name': 'Eric', 'description': 'American · masculine', 'lang': 'en-us'},
    {'id': 'bf_emma', 'name': 'Emma', 'description': 'British · feminine · classic', 'lang': 'en-gb'},
    {'id': 'bf_isabella', 'name': 'Isabella', 'description': 'British · feminine', 'lang': 'en-gb'},
    {'id': 'bm_george', 'name': 'George', 'description': 'British · masculine', 'lang': 'en-gb'},
    {'id': 'bm_daniel', 'name': 'Daniel', 'description': 'British · masculine', 'lang': 'en-gb'},
    {'id': 'bm_lewis', 'name': 'Lewis', 'description': 'British · masculine', 'lang': 'en-gb'},
]
STYLES = [
    {'id': 'natural', 'name': 'Natural', 'description': 'An even pace with standard pauses.', 'speed': 1.0, 'pause': 0.22},
    {'id': 'unhurried', 'name': 'Unhurried', 'description': 'A little slower, with room between passages.', 'speed': 0.90, 'pause': 0.42},
    {'id': 'brisk', 'name': 'Brisk', 'description': 'A quicker pace and shorter pauses.', 'speed': 1.10, 'pause': 0.14},
]
PREVIEW_TEXT = 'At the end of the garden, a small wooden gate opened onto the hills. The morning was quiet. She put the book in her pocket, took one last look at the house, and set off. Somewhere beyond the trees, a new story was waiting to be told.'
MODEL = None
LOCK = threading.Lock()
ENGINE_VERSION = 'kokoro-onnx-' + importlib.metadata.version('kokoro-onnx')


def cpu_descriptor():
    return {'runtime':ENGINE_VERSION,'model':CONFIG['model_sha256'],'voices':CONFIG['voices_sha256']}


def engine_descriptor(options=None):
    selected=(options or {}).get('_engine') or cpu_descriptor()
    native=native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])
    if selected not in (cpu_descriptor(),native):
        raise ValueError('This job requires its original speech engine/model. Start a new edition to change it.')
    return dict(selected)


def is_native(options):
    return (options.get('_engine') or {}).get('runtime')==NATIVE_RUNTIME


def default_engine():
    if CONFIG.get('speech_backend')=='native_cuda':
        import native_speech
        if native_speech.available():return native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])
    return cpu_descriptor()


def engine_label():
    return 'Kokoro · local GPU preferred' if CONFIG.get('speech_backend')=='native_cuda' else 'Kokoro · local CPU'


def capabilities(options=None):
    return {'id': 'kokoro', 'runtime': engine_descriptor(options)['runtime'], 'sample_rate': 24000,
            'direction': False, 'cloning': False, 'pace': True, 'pronunciation': 'respelling'}


def profile_snapshot(options):
    # A JSON snapshot is saved with the plan; callers cannot mutate it by editing options.
    engine=engine_descriptor(options)
    result = {'schema': 1, 'engine': capabilities(options), 'model_sha256': engine['model'],
            'voices_sha256': CONFIG['voices_sha256'], 'voice': dict(voice_info(options['voice'])),
            'style': dict(style_info(options['style'])), 'pace': options['pace'],
            'effective_speed': effective_speed(options), 'pause_policy': 'paragraph' if options.get('pause_mode')=='paragraph' else 'legacy_chunk',
            'skip_notes': options['skip_notes'], 'skip_frontmatter': options['skip_frontmatter']}
    for key in ('paragraph_pause','chapter_pause','pronunciations','profile','_dictionaries'):
        if key in options:result[key]=options[key]
    if options.get('_occurrences'):
        result['context_pronunciations']={'renderer':options['_context_renderer'],
            'occurrences':options['_occurrences'],'approvals':options.get('_annotation_provenance',[])}
    if is_native(options):result['renderer']=engine
    if options.get('_pause_policy'):result['contextual_pauses']=options['_pause_policy']
    return result


def voice_info(voice):
    return next(v for v in VOICES if v['id'] == voice)


def style_info(style):
    return next(s for s in STYLES if s['id'] == style)


def effective_speed(options):
    return max(0.5, min(2.0, options['pace'] * style_info(options['style'])['speed']))


def cache_key(bid, options, source_stamp='', extraction_version=EXTRACTOR_VERSION):
    audio_options={k:v for k,v in options.items() if k not in {'_engine','_dictionaries','_profile_pronunciations','profile','_annotation_provenance'}}
    value = {'book': bid, 'options': audio_options, 'engine': ENGINE_VERSION, 'model': CONFIG['model_sha256'], 'voices': CONFIG['voices_sha256'], 'source': source_stamp, 'extractor': extraction_version}
    engine=engine_descriptor(options)
    if is_native(options):value.update(engine=engine['runtime'],model=engine['model'],renderer=engine)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load():
    global MODEL
    if MODEL is None:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        for name,expected in [('kokoro-v1.0.onnx',CONFIG['model_sha256']),('voices-v1.0.bin',CONFIG['voices_sha256'])]:
            with (MODELS/name).open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
                    raise ValueError('The original speech model or voice file changed; restore its pinned artifact.')
        options = ort.SessionOptions()
        options.intra_op_num_threads = CONFIG.get('cpu_threads', 6)
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(MODELS / 'kokoro-v1.0.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
        MODEL = Kokoro.from_session(session, str(MODELS / 'voices-v1.0.bin'))
    return MODEL


def generate(text, options, path):
    import numpy as np
    import soundfile as sf
    start = time.perf_counter()
    engine=engine_descriptor(options)
    source_text=text
    occurrences=options.get('_chunk_occurrences',[])
    if occurrences and options.get('_context_renderer')!=1:
        raise ValueError('This narration requires its original context-pronunciation renderer.')
    text,mapped,skipped=pronunciation.remap(text,options.get('pronunciations',[]),occurrences)
    phone_input=None;outcomes=[]
    with LOCK:
        k = load()
        lang=voice_info(options['voice'])['lang']
        if mapped:phone_input,outcomes=occurrence_phones.align(text,mapped,k.tokenizer,lang=lang)
        arguments={'voice':options['voice'],'speed':effective_speed(options),'lang':lang}
        if phone_input is not None:arguments['is_phonemes']=True
        capture=bool(options.get('_pause_policy'))
        spoken=[]
        if is_native(options):
            import native_speech
            if capture:
                audio,rate,spoken=native_speech.render(k,phone_input if phone_input is not None else text,arguments,engine,return_timings=True)
            else:audio,rate=native_speech.render(k,phone_input if phone_input is not None else text,arguments,engine)
        else:
            if capture:audio,rate,spoken=k.create_timed(phone_input if phone_input is not None else text,**arguments)
            else:audio, rate = k.create(phone_input if phone_input is not None else text,**arguments)
    if len(audio) < rate * 0.1 or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 0.0001:
        raise ValueError('The speech engine produced invalid or silent audio.')
    pause=options.get('_pause_seconds',style_info(options['style'])['pause'])
    audio = np.concatenate([audio, np.zeros(int(rate * pause), dtype=np.float32)])
    temp = path.with_suffix('.tmp.wav')
    sf.write(str(temp), audio, rate, subtype='PCM_16')
    temp.replace(path)
    if capture:
        from context_pauses import file_hash
        target=path.with_suffix('.timings.json');temporary=target.with_suffix('.tmp.json')
        timing_receipt={'schema':1,'input_sha256':digest({'text':source_text,'policy':options['_pause_policy']}),
            'wave_sha256':file_hash(path),'timings':[{'phoneme':t.phoneme,'start':t.start,'end':t.end} for t in spoken]}
        timing_receipt['sha256']=digest(timing_receipt)
        temporary.write_text(json.dumps(timing_receipt),encoding='utf-8')
        temporary.replace(target)
    if is_native(options):
        with path.open('rb') as stream:wave_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        target=path.with_suffix('.engine.json');temporary=target.with_suffix('.tmp.json')
        temporary.write_text(json.dumps({'schema':1,'engine':engine,'input_sha256':context_identity(source_text,options),
                                         'wave_sha256':wave_sha}),encoding='utf-8');temporary.replace(target)
    if occurrences:
        with path.open('rb') as stream:wave_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        receipt={'schema':1,'input_sha256':context_identity(source_text,options),'wave_sha256':wave_sha,
                 'outcomes':skipped+outcomes,'phone_input_sha256':hashlib.sha256(phone_input.encode()).hexdigest() if phone_input else None}
        target=path.with_suffix('.pronunciation.json');temporary=target.with_suffix('.tmp.json')
        temporary.write_text(json.dumps(receipt),encoding='utf-8');temporary.replace(target)
    return len(audio) / rate, time.perf_counter() - start


def context_identity(text, options):
    value={'schema':1,'text':text,'occurrences':options.get('_chunk_occurrences',[]),
                   'dictionary':options.get('pronunciations',[]),'voice':options['voice'],
                   'style':options['style'],'pace':options['pace'],'pause':options.get('_pause_seconds'),
                   'engine':ENGINE_VERSION,'model':CONFIG['model_sha256'],'renderer':options.get('_context_renderer')}
    if is_native(options):value['speech_renderer']=engine_descriptor(options)
    if options.get('_pause_policy'):value['pause_policy']=options['_pause_policy']
    return digest(value)


def validate_cached(text,options,path):
    if options.get('_pause_policy'):
        from context_pauses import file_hash
        row=json.loads(path.with_suffix('.timings.json').read_text(encoding='utf-8'))
        if (row.get('sha256')!=digest({k:v for k,v in row.items() if k!='sha256'})
            or row.get('input_sha256')!=digest({'text':text,'policy':options['_pause_policy']}) or row.get('wave_sha256')!=file_hash(path)):
            raise ValueError('The cached pause timing input/audio changed.')
    if is_native(options):
        row=json.loads(path.with_suffix('.engine.json').read_text(encoding='utf-8'))
        with path.open('rb') as stream:wave_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        if row.get('schema')!=1 or row.get('engine')!=engine_descriptor(options) or row.get('input_sha256')!=context_identity(text,options) or row.get('wave_sha256')!=wave_sha:
            raise ValueError('The cached GPU audio or renderer identity changed.')
    return context_receipt(text,options,path)


def context_receipt(text,options,path):
    if not options.get('_chunk_occurrences'):return None
    row=json.loads(path.with_suffix('.pronunciation.json').read_text(encoding='utf-8'))
    with path.open('rb') as stream:wave_sha=hashlib.file_digest(stream,'sha256').hexdigest()
    if row.get('schema')!=1 or row.get('input_sha256')!=context_identity(text,options) or row.get('wave_sha256')!=wave_sha:
        raise ValueError('The reviewed pronunciation audio or input changed.')
    expected={o['id'] for o in options['_chunk_occurrences']}
    if {o['id'] for o in row['outcomes']}!=expected:
        raise ValueError('The pronunciation receipt omits a reviewed occurrence.')
    return row
