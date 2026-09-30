"""Non-destructive, repeatable import of the previous local SQLite database."""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
import run_local
from app.main import app
from app.db.base import Base
from app.db.session import engine, SessionLocal
from app.models import Incident, Report, StatusUpdate, IncidentStatus
from app.services.repository import get_or_create_building, get_or_create_user
from app.services.classifier import classify
from sqlalchemy import select

source=Path(sys.argv[1]).resolve()
old=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
old.row_factory=sqlite3.Row
Base.metadata.create_all(engine)
count=0
with SessionLocal() as db:
    user=get_or_create_user(db,'legacy-local-resident')
    for item in old.execute('SELECT * FROM incidents'):
        marker='legacy:'+item['id']
        if db.scalar(select(StatusUpdate).where(StatusUpdate.source=='legacy_import',StatusUpdate.note==marker)): continue
        building=get_or_create_building(db,item['building_id'])
        state=IncidentStatus({'open':'new','in_progress':'in_progress','closed':'resolved'}[item['status']])
        incident=Incident(building_id=building.id,category=classify(item['description']),description=item['description']+(f"\nМесто: {item['location_hint']}" if item['location_hint'] else ''),status=state,created_at=datetime.fromisoformat(item['created_at']),last_status_changed_at=datetime.fromisoformat(item['updated_at']))
        if state==IncidentStatus.RESOLVED: incident.closed_at=datetime.fromisoformat(item['updated_at'])
        db.add(incident);db.flush()
        for report in old.execute('SELECT * FROM reports WHERE incident_id=?',(item['id'],)):
            db.add(Report(incident_id=incident.id,user_id=user.id,message_text=report['description'],submitted_at=datetime.fromisoformat(report['created_at'])))
        db.add(StatusUpdate(incident_id=incident.id,status=state,source='legacy_import',note=marker))
        count+=1
    db.commit()
print(f'Imported incidents: {count}')
