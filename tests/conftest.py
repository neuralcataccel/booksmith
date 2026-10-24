import pytest


@pytest.fixture(autouse=True)
def offline_speech_default(monkeypatch):
    # Application API tests must never probe/admit work through a live GPU
    # deployment. Native-specific tests opt in with explicitly mocked admission.
    import speech
    monkeypatch.setitem(speech.CONFIG,'speech_backend','cpu')
    monkeypatch.setitem(speech.CONFIG,'contextual_pauses',False)
    monkeypatch.delitem(speech.CONFIG,'jev_monthly_limit_usd',raising=False)
    monkeypatch.delitem(speech.CONFIG,'jev_owner_limit_usd',raising=False)
