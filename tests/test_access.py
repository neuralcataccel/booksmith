import base64
import hashlib
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app
import catalogue
import store
import worker
from access import AccessControl


def credentials(user='family', password='test-password'):
    return 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()


@pytest.fixture
def protected(tmp_path, monkeypatch):
    import ipaddress
    monkeypatch.setattr(app,'NETWORKS',[ipaddress.ip_network(v) for v in ['127.0.0.0/8','192.0.2.0/24','::1/128']])
    monkeypatch.setattr(app,'HOSTS',{'localhost','127.0.0.1','192.0.2.60','[::1]'})
    monkeypatch.setattr(app,'PUBLIC_HOST','audio.example.invalid')
    monkeypatch.setattr(app,'PROXY_PEERS',{'192.0.2.6'})
    salt = b'unit-test-salt'
    config = {'username': 'family', 'salt': salt.hex(), 'iterations': 1000, 'password_hash': hashlib.pbkdf2_hmac('sha256', b'test-password', salt, 1000).hex(), 'gateway_token': 'test-gateway-token'}
    auth = AccessControl(settings=config)
    monkeypatch.setattr(app, 'AUTH', auth)
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'test.sqlite')
    monkeypatch.setattr(store, 'prepare_dirs', lambda: None)
    monkeypatch.setattr(catalogue, 'sync', lambda: None)
    monkeypatch.setattr(worker, 'start', lambda: None)
    monkeypatch.setattr(worker, 'stop', lambda: None)
    return auth


def test_auth_covers_pages_api_covers_and_audio(protected):
    with TestClient(app.app, base_url='http://localhost:8787', client=('127.0.0.1', 50000)) as c:
        for path in ['/', '/static/app.js', '/api/config', '/api/books/1/cover', '/api/jobs/any/audio']:
            r = c.get(path)
            assert r.status_code == 401
            assert 'Booksmith' in r.headers['www-authenticate']
        assert c.get('/api/config', headers={'Authorization': credentials()}).status_code == 200
        assert c.get('/healthz').status_code == 200


def test_public_requests_need_real_proxy_and_gateway(protected):
    headers = {'Authorization': credentials(), 'X-Booksmith-Gateway': 'test-gateway-token', 'X-Forwarded-Proto': 'https'}
    with TestClient(app.app, base_url='https://audio.example.invalid', client=('192.0.2.60', 50000)) as c:
        assert c.get('/api/config', headers=headers).status_code == 403
    with TestClient(app.app, base_url='https://audio.example.invalid', client=('192.0.2.6', 50000)) as c:
        assert c.get('/api/config', headers={'Authorization': credentials()}).status_code == 403
        assert c.get('/api/config', headers=headers).status_code == 200
        assert c.get('/healthz', headers=headers).status_code == 404


def test_https_origin_works_without_trusting_spoofed_proto(protected):
    headers = {'Authorization': credentials(), 'X-Booksmith-Gateway': 'test-gateway-token', 'X-Forwarded-Proto': 'https', 'CF-Connecting-IP': '198.51.100.9'}
    with TestClient(app.app, base_url='http://audio.example.invalid', client=('192.0.2.6', 50000)) as c:
        config = c.get('/api/config', headers=headers).json()
        headers.update({'X-Booksmith-Token': config['csrf'], 'Origin': 'https://audio.example.invalid'})
        # Validation reaches the handler, proving the HTTPS origin was accepted over the HTTP backend.
        assert c.post('/api/previews', json={'voice': 'invalid'}, headers=headers).status_code == 422
        headers['Origin'] = 'https://other.example'
        assert c.post('/api/previews', json={}, headers=headers).status_code == 403
        headers['Origin'] = 'https://audio.example.invalid'
        headers['X-Forwarded-Proto'] = 'http'
        r = c.get('/api/config', headers=headers, follow_redirects=False)
        assert r.status_code == 308
        assert r.headers['location'] == 'https://audio.example.invalid/api/config'


def test_bad_passwords_are_throttled_but_no_auth_challenges_are_not(protected):
    with TestClient(app.app, base_url='http://localhost:8787', client=('127.0.0.1', 50000)) as c:
        for _ in range(15):
            assert c.get('/api/config').status_code == 401
        for _ in range(8):
            assert c.get('/api/config', headers={'Authorization': credentials(password='wrong')}).status_code == 401
        assert c.get('/api/config', headers={'Authorization': credentials(password='wrong')}).status_code == 429
        assert c.get('/healthz').status_code == 200


def test_forwarded_client_ip_cannot_be_spoofed_by_lan_client(protected):
    with TestClient(app.app, base_url='http://localhost:8787', client=('192.0.2.60', 50000)) as c:
        for i in range(8):
            assert c.get('/api/config', headers={'Authorization': credentials(password='wrong'), 'CF-Connecting-IP': f'198.51.100.{i}'}).status_code == 401
        assert c.get('/api/config', headers={'Authorization': credentials(), 'CF-Connecting-IP': '203.0.113.99'}).status_code == 429
