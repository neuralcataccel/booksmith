"""Refresh HTML asset versions, or fail a preflight if a JS/CSS reference is stale."""
import argparse
import hashlib
from pathlib import Path
import re


def refresh(root, check=False):
    index=root/'index.html'
    original=index.read_text(encoding='utf-8')
    def reference(match):
        name=match.group(1)
        version=hashlib.sha256((root/name).read_bytes()).hexdigest()[:12]
        return '/static/'+name+'?v='+version
    revised=re.sub(r'/static/([\w.-]+\.(?:js|css))(?:\?v=[a-z0-9]+)?',reference,original)
    if check and revised!=original:raise ValueError('JS/CSS changed. Run refresh_assets.py before deployment.')
    if not check and revised!=original:index.write_text(revised,encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-root',type=Path,default=Path(__file__).resolve().parent/'web')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args();refresh(args.web_root,args.check)
