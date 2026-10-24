import hashlib
import json
import re
import posixpath
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote, urldefrag

from bs4 import BeautifulSoup
from ebooklib import epub

from settings import CONFIG, LIBRARY, WORK

EXTRACTOR_VERSION = '7'
BLOCKS = {'h1', 'h2', 'h3', 'h4', 'p', 'li', 'blockquote', 'pre'}


def split_text(text, max_chars=360):
    """Split on sentence/word boundaries; never truncate text to fit a model."""
    text = re.sub(r'\s+', ' ', text).strip()
    pieces = []
    while len(text) > max_chars:
        window = text[:max_chars + 1]
        boundaries = list(re.finditer(r'[.!?][\"\u201d\u2019\)]?\s+', window))
        end = boundaries[-1].end() if boundaries and boundaries[-1].end() > max_chars // 3 else window.rfind(' ')
        if end <= 0:
            end = max_chars
        pieces.append(text[:end].strip())
        text = text[end:].strip()
    if text:
        pieces.append(text)
    return pieces


def html_body(content, skip_notes=True):
    if isinstance(content, bytes):
        content = re.sub(br'^\s*<\?xml[^>]*\?>', b'', content, count=1)
    soup = BeautifulSoup(content, 'lxml')
    for tag in list(soup.find_all(['script', 'style', 'nav', 'header', 'footer'])):
        tag.decompose()
    if skip_notes:
        for tag in list(soup.find_all(True)):
            if not tag.attrs:
                continue
            types = str(tag.get('epub:type', '')).split()
            role = tag.get('role', '')
            if set(types) & {'footnote', 'endnote', 'rearnote', 'noteref'} or role in {'doc-footnote', 'doc-endnote', 'doc-noteref'}:
                tag.decompose()
    return soup.body or soup


def html_blocks(content, skip_notes=True):
    body = html_body(content, skip_notes)
    title = ''
    heading = body.find(['h1', 'h2', 'h3'])
    if heading:
        title = heading.get_text(' ', strip=True)
    blocks = []
    for node in body.find_all(list(BLOCKS)):
        if any(p.name in BLOCKS for p in node.parents):
            continue
        text = node.get_text(' ', strip=True)
        if text:
            blocks.append(text)
    if not blocks:
        text = body.get_text(' ', strip=True)
        if text:
            blocks = [text]
    return title, blocks


def load_epub(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 15000 or sum(i.file_size for i in infos) > 500 * 1024 * 1024:
            raise ValueError('This EPUB is too large for the proof of concept.')
        if 'META-INF/encryption.xml' in archive.namelist():
            encryption = archive.read('META-INF/encryption.xml')
            # Obfuscated fonts are common and do not mean the book text is DRM protected.
            if b'http://www.idpf.org/2008/embedding' not in encryption and b'http://ns.adobe.com/pdf/enc#RC' not in encryption:
                raise ValueError('This EPUB contains encrypted content. Use a readable, DRM-free edition.')
    return epub.read_epub(str(path), options={'ignore_ncx': True})


def read_epub_legacy(path, skip_notes=True, skip_frontmatter=True):
    """Version 4 remains readable only to identify already synthesized chunks."""
    book = load_epub(path)
    toc_titles = {}

    def walk_toc(entries):
        for entry in entries:
            if isinstance(entry, tuple):
                walk_toc([entry[0]])
                walk_toc(entry[1])
            elif hasattr(entry, 'href'):
                toc_titles.setdefault(entry.href.split('#')[0], entry.title)

    walk_toc(book.toc)
    chapters = []
    for item_id, linear in book.spine:
        if linear == 'no':
            continue
        item = book.get_item_with_id(item_id)
        if not item or not hasattr(item, 'get_body_content'):
            continue
        name = item.get_name()
        if isinstance(item, epub.EpubNav) or name.lower().endswith(('nav.xhtml', 'toc.xhtml', 'toc.html')):
            continue
        heading, blocks = html_blocks(item.get_content(), skip_notes)
        toc_title = toc_titles.get(name, '')
        if skip_frontmatter:
            leaf = Path(name).stem.lower()
            labels = [re.sub(r'[^a-z ]', '', x.lower()).strip() for x in [heading, toc_title, *(blocks[:1])]]
            if leaf in {'cover', 'titlepage', 'title_page', 'copyright', 'colophon', 'toc', 'nav'} or any(x in {'copyright', 'contents', 'table of contents', 'title page', 'epubbooks information'} for x in labels):
                continue
        if not blocks:
            continue
        # Gutenberg license pages are outside the work, not narration material.
        if skip_frontmatter and any('project gutenberg license' in t.lower() or 'full project gutenberg' in t.lower() for t in blocks[:2]):
            continue
        chunks = [chunk for block in blocks for chunk in split_text(block)]
        if chunks:
            title = heading or toc_title or f'Part {len(chapters) + 1}'
            title = re.sub(r'^\{chapter-(\d+)\}$', r'Chapter \1', title, flags=re.IGNORECASE)
            chapters.append({'title': title[:180], 'chunks': chunks})
    if not chapters:
        raise ValueError('No readable text was found in this ebook.')
    if sum(len(t) for c in chapters for t in c['chunks']) > 4_000_000:
        raise ValueError('This book exceeds the proof-of-concept text limit. Choose a shorter edition.')
    return chapters


def read_epub(path, skip_notes=True, skip_frontmatter=True, document_fallback=True, include_structure=False, body_fallback=True):
    """Use navigation boundaries in spine order, retaining continuation documents."""
    book = load_epub(path)
    navigation = {}

    def walk(entries):
        for entry in entries:
            if isinstance(entry, tuple):
                walk([entry[0]])
                walk(entry[1])
            elif hasattr(entry, 'href'):
                document, fragment = urldefrag(unquote(entry.href))
                navigation.setdefault(posixpath.normpath(document), {}).setdefault(fragment, entry.title)
    walk(book.toc)
    reliable_navigation = sum(len(links) for links in navigation.values()) >= 2
    inferred_chapter = False
    chapters = []
    for spine_index, (item_id, linear) in enumerate(book.spine):
        if linear == 'no':
            continue
        item = book.get_item_with_id(item_id)
        if not item or not hasattr(item, 'get_body_content'):
            continue
        name = item.get_name()
        if isinstance(item, epub.EpubNav) or name.lower().endswith(('nav.xhtml', 'toc.xhtml', 'toc.html')):
            continue
        heading, blocks = html_blocks(item.get_content(), skip_notes)
        links = navigation.get(posixpath.normpath(name), {})
        title = links.get('', '')
        if skip_frontmatter:
            labels = [re.sub(r'[^a-z ]', '', x.lower()).strip() for x in [heading, title, *(blocks[:1])]]
            if Path(name).stem.lower() in {'cover', 'titlepage', 'title_page', 'copyright', 'colophon', 'toc', 'nav'} or any(x in {'copyright', 'contents', 'table of contents', 'title page', 'epubbooks information'} for x in labels):
                continue
            if any('project gutenberg license' in t.lower() or 'full project gutenberg' in t.lower() for t in blocks[:2]):
                continue
        if not blocks:
            continue
        body = html_body(item.get_content(), skip_notes)
        nodes = [node for node in body.find_all(list(BLOCKS)) if not any(p.name in BLOCKS for p in node.parents)]
        if body_fallback:
            # Some converted EPUBs put prose in divs and retain empty p elements.
            # Empty nodes must not mask html_blocks' body-text fallback.
            nodes = [node for node in nodes if node.get_text(' ',strip=True)]
        positions = {id(node): index for index, node in enumerate(body.find_all(True))}
        anchors = []
        for fragment, label in links.items():
            if not fragment:
                continue
            target = body.find(id=fragment) or body.find(attrs={'name': fragment})
            if target is not None:
                anchors.append((positions.get(id(target), -1), label))
        anchors.sort(key=lambda x: x[0])

        def begin(label):
            label = re.sub(r'^\{chapter-(\d+)\}$', r'Chapter \1', label, flags=re.IGNORECASE)
            chapters.append({'title': label[:180], 'chunks': []})

        def append_block(text, index, kind):
            pieces = split_text(text)
            if not pieces:
                return
            chapter = chapters[-1]
            start = len(chapter['chunks'])
            chapter['chunks'].extend(pieces)
            if include_structure:
                chapter.setdefault('blocks', []).append({
                    'text': re.sub(r'\s+', ' ', text).strip(), 'kind': kind,
                    'locator': {'document': name, 'spine': spine_index, 'block': index},
                    'chunk_start': start, 'chunk_count': len(pieces)})

        if title:
            begin(title)  # The book's TOC takes precedence over an in-story heading.
            inferred_chapter = bool(re.match(r'^chapter\s+(?:\d+|[ivxlcdm]+)\b', title, re.IGNORECASE))
        elif not chapters:
            begin(heading or f'Part {len(chapters) + 1}')
            inferred_chapter = bool(re.match(r'^chapter\s+(?:\d+|[ivxlcdm]+)\b', heading, re.IGNORECASE))
        elif not links and re.match(r'^chapter\s+(?:\d+|[ivxlcdm]+)\b', heading, re.IGNORECASE):
            begin(heading)
            inferred_chapter = True
        elif document_fallback and not reliable_navigation and not inferred_chapter:
            # Sparse/missing TOCs are not evidence that every file is one chapter.
            begin(heading or f'Part {len(chapters) + 1}')
        # No new navigation boundary means this document continues the prior chapter.
        if not nodes:
            for index, text in enumerate(blocks):
                append_block(text, index, 'body')
            continue
        for index, node in enumerate(nodes):
            end = max([positions[id(node)], *[positions[id(child)] for child in node.find_all(True)]])
            while anchors and anchors[0][0] <= end:
                _, label = anchors.pop(0)
                if chapters[-1]['chunks']:
                    begin(label)
                else:
                    chapters[-1]['title'] = label[:180]
            text = node.get_text(' ', strip=True)
            if text:
                append_block(text, index, node.name)
    chapters = [chapter for chapter in chapters if chapter['chunks']]
    if not chapters:
        raise ValueError('No readable text was found in this ebook.')
    if sum(len(t) for c in chapters for t in c['chunks']) > 4_000_000:
        raise ValueError('This book exceeds the proof-of-concept text limit. Choose a shorter edition.')
    return chapters


def extract_book(source, options, version=EXTRACTOR_VERSION, include_structure=False):
    stat = source.stat()
    fingerprint = f'{source}|{stat.st_size}|{stat.st_mtime_ns}|{version}|{options["skip_notes"]}|{options["skip_frontmatter"]}'
    conversion_key = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    key = hashlib.sha256((fingerprint + '|structure=1').encode()).hexdigest()[:24] if include_structure else conversion_key
    root = WORK / 'extracted'
    root.mkdir(exist_ok=True)
    cached = root / f'{key}.json'
    if cached.exists():
        return json.loads(cached.read_text(encoding='utf-8'))
    if source.suffix.lower() == '.txt':
        try:
            text = source.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            text = source.read_text(encoding='cp1252')
        chunks, blocks = [], []
        for index, paragraph in enumerate(re.split(r'\n\s*\n', text)):
            pieces = split_text(paragraph)
            if not pieces:
                continue
            blocks.append({'text': re.sub(r'\s+', ' ', paragraph).strip(), 'kind': 'p',
                           'locator': {'document': source.name, 'spine': 0, 'block': index},
                           'chunk_start': len(chunks), 'chunk_count': len(pieces)})
            chunks.extend(pieces)
        chapters = [{'title': 'Full text', 'chunks': chunks}]
        if include_structure:
            chapters[0]['blocks'] = blocks
    else:
        epub_path = source
        if source.suffix.lower() in {'.mobi', '.azw3'}:
            exchange = Path(CONFIG['calibre_exchange'])
            exchange.mkdir(parents=True, exist_ok=True)
            epub_path = exchange / f'{conversion_key}.epub'
            if not epub_path.exists():
                relative = source.relative_to(LIBRARY).as_posix()
                cmd = ['docker', 'exec', CONFIG['calibre_container'], 'ebook-convert', '/books/' + relative, f'/config/ebook-audio-staging/{conversion_key}.epub']
                result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode != 0 or not epub_path.is_file():
                    epub_path.unlink(missing_ok=True)
                    raise ValueError('Calibre could not convert this ebook. It may be encrypted or damaged. Try another edition.')
        if version == '4':
            chapters = read_epub_legacy(epub_path, options['skip_notes'], options['skip_frontmatter'])
        else:
            chapters = read_epub(epub_path, options['skip_notes'], options['skip_frontmatter'], document_fallback=version != '5', include_structure=include_structure, body_fallback=version not in {'5','6'})
    result = {'key': key, 'version': version, 'chapters': chapters, 'characters': sum(len(t) for c in chapters for t in c['chunks'])}
    temp = cached.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temp.replace(cached)
    return result
