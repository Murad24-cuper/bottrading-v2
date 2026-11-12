"""Every dashboard page renders on desktop and phone views without a Streamlit exception."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / 'src' / 'dashboard' / 'app.py'


@pytest.mark.parametrize('view', [None, 'phone'])
@pytest.mark.parametrize('page', ['overview', 'positions', 'history', 'settings', 'brain', 'system'])
def test_page_renders(page, view, tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'bot.db'))
    monkeypatch.delenv('DASHBOARD_PASSWORD', raising=False)
    from src.db import Database
    Database(tmp_path / 'bot.db')
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.query_params['page'] = page
    if view: app.query_params['view'] = view
    app.run()
    assert not app.exception, [str(item.value) for item in app.exception]


def test_settings_page_has_save_button(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'bot.db'))
    monkeypatch.delenv('DASHBOARD_PASSWORD', raising=False)
    from src.db import Database
    Database(tmp_path / 'bot.db')
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.query_params['page'] = 'settings'
    app.run()
    labels = [button.label for button in app.button]
    assert any('Yadda saxla' in label for label in labels)
