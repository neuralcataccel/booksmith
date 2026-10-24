"""Shared family HTTP authentication. Secret material stays outside the source tree."""
import base64
import collections
import functools
import hashlib
import json
import secrets
import threading
import time

from settings import RUNTIME


class AccessControl:
    def __init__(self, path=None, settings=None):
        if settings is None:
            path = path or RUNTIME / 'data' / 'access.json'
            settings = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        self.settings = settings
        self.enabled = bool(settings.get('username') and settings.get('password_hash') and settings.get('gateway_token'))
        self.failures = collections.OrderedDict()
        self.lock = threading.Lock()
        self.verify = functools.lru_cache(maxsize=16)(self._verify_success)

    def gateway_valid(self, value):
        expected = self.settings.get('gateway_token', '')
        return bool(expected) and secrets.compare_digest(value.encode('utf-8'), expected.encode('utf-8'))

    def limited(self, client):
        now = time.monotonic()
        with self.lock:
            attempts = self.failures.get(client, [])
            recent = [t for t in attempts if now - t < 900]
            if recent:
                self.failures[client] = recent
            else:
                self.failures.pop(client, None)
            return len(recent) >= 8

    def failed(self, client):
        with self.lock:
            self.failures.setdefault(client, []).append(time.monotonic())
            self.failures.move_to_end(client)
            while len(self.failures) > 4096:
                self.failures.popitem(last=False)

    def succeeded(self, client):
        with self.lock:
            self.failures.pop(client, None)

    def _verify_success(self, header):
        # Invalid credentials raise so the cache retains successes only.
        scheme, _, value = header.partition(' ')
        if scheme.lower() != 'basic' or len(value) > 2048:
            raise ValueError('Invalid credentials')
        try:
            decoded = base64.b64decode(value, validate=True).decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid credentials') from exc
        username, separator, password = decoded.partition(':')
        if not separator or not secrets.compare_digest(username.encode('utf-8'), self.settings['username'].encode('utf-8')):
            raise ValueError('Invalid credentials')
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(self.settings['salt']), self.settings['iterations']).hex()
        if not secrets.compare_digest(digest, self.settings['password_hash']):
            raise ValueError('Invalid credentials')
        return True

    def authenticated(self, header):
        try:
            return self.verify(header)
        except (ValueError, KeyError):
            return False


AUTH = AccessControl()
