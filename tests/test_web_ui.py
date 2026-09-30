from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.db.base import Base
from app.db.session import get_db
from app.core.config import get_settings
import app.api.webhook as hooks

@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT','local')
    monkeypatch.setenv('MAX_BOT_TOKEN','')
    monkeypatch.setenv('WEBHOOK_SHARED_SECRET','')
    get_settings.cache_clear()
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    factory=sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    def db():
        with factory() as session: yield session
    previous=dict(app.dependency_overrides)
    app.dependency_overrides[get_db]=db
    monkeypatch.setattr(hooks,'SessionLocal',factory)
    yield TestClient(app)
    app.dependency_overrides=previous
    get_settings.cache_clear()
    engine.dispose()

def draft(**kw):
    data=dict(building_id='Тестовый дом 1',text='Не работает лифт',request_id=str(uuid4()))
    data.update(kw)
    return data

def test_bot_and_web_share_incident_and_status(ui):
    ui.post('/webhook/address-confirmation',json={'max_user_id':'bot-test','building_address':'Тестовый дом 1'})
    bot=ui.post('/webhook/message',json={'max_user_id':'bot-test','text':'Не работает лифт'}).json()
    rows=ui.get('/api/incidents').json()
    assert rows[0]['id']==str(bot['incident_id'])
    data=draft()
    review=ui.post('/api/analyze',json=data).json()
    assert review['action']=='attach'
    data.update(choice='confirm',incident_id=review['incident_id'])
    assert ui.post('/api/submit',json=data).json()['saved']
    assert ui.post('/api/submit',json=data).json()['saved']
    assert len(ui.get('/api/reports',params={'id':review['incident_id']}).json())==2
    assert ui.post('/api/status',json={'id':review['incident_id'],'status':'closed'}).status_code==200
    assert 'решена' in ui.post('/webhook/status-query',json={'max_user_id':'bot-test'}).json()['reply_text'].lower() or ui.get('/api/incidents').json()[0]['status']=='closed'

def test_profile_and_new_separate(ui):
    assert ui.post('/api/profile',json={'address':'Тестовый дом 1'}).status_code==200
    assert ui.get('/api/profile').json()['address']=='тестовый дом 1'
    data=draft()
    assert ui.post('/api/submit',json=data).json()['saved']
    data2=draft(choice='new')
    assert ui.post('/api/submit',json=data2).json()['saved']
    assert len(ui.get('/api/incidents').json())==2
    data['text']='Другой текст'
    assert ui.post('/api/submit',json=data).status_code==409
    ui.post('/api/profile',json={'address':''})
    assert ui.get('/api/profile').json()['address']==''

def test_stale_review_and_urgent(ui):
    data=draft()
    assert ui.post('/api/analyze',json=data).json()['action']=='create'
    ui.post('/api/submit',json=data)
    assert ui.post('/api/submit',json=draft()).json()['changed']
    assert ui.post('/api/analyze',json={**draft(),'text':'В подъезде запах газа','category':'other'}).json()['urgent']

def test_uncertain_web_match_requires_explicit_choice(ui):
    first=draft(text='Не работает лифт в первом подъезде')
    assert ui.post('/api/submit',json=first).json()['saved']
    second=draft(text='Лифт снова стоит, не работает уже второй день')
    review=ui.post('/api/analyze',json=second).json()
    assert review['action']=='confirm'
    second['incident_id']=review['incident_id']
    assert ui.post('/api/submit',json=second).json()['saved'] is False
    second['choice']='confirm'
    assert ui.post('/api/submit',json=second).json()['saved'] is True

def test_web_match_does_not_cross_entrances(ui):
    assert ui.post('/api/submit',json=draft(text='Не работает лифт',location_hint='подъезд 1')).json()['saved']
    review=ui.post('/api/analyze',json=draft(text='Не работает лифт',location_hint='подъезд 2')).json()
    assert review['action']=='create'
    assert review['candidate'] is None

def test_local_boundary(ui,monkeypatch):
    assert ui.get('/api/incidents',headers={'Host':'attacker.example'}).status_code==403
    assert ui.post('/api/profile',json={'address':'Дом 1'},headers={'Origin':'https://attacker.example'}).status_code==403
    monkeypatch.setenv('ENVIRONMENT','production');get_settings.cache_clear()
    assert ui.get('/api/incidents').status_code==403

def test_assets_and_validation(ui):
    assert ui.get('/').status_code==200
    assert ui.get('/app.js').status_code==200
    assert ui.post('/api/submit',json={'building_id':'','text':'x'}).status_code==422

def test_roles_demo_and_single_saved_address_flow(ui):
    html=ui.get('/').text
    script=ui.get('/app.js').text
    assert 'id="nav-resident"' in html and 'id="nav-dispatch"' in html
    assert 'id="demo-view"' in html and 'Демонстрационные данные' in html
    assert 'id="building-field"' in html
    assert "$('building-field').hidden = Boolean(savedHome)" in script
