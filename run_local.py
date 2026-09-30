"""Local combined app; never exposes the unauthenticated demo panel publicly."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
(ROOT/'data').mkdir(exist_ok=True)
os.environ['ENVIRONMENT']='local'
os.environ.setdefault('DATABASE_URL','sqlite:///'+str(ROOT/'data/domradar.sqlite3').replace('\\','/'))

if __name__ == '__main__':
    from app.main import app
    from app.db.base import Base
    from app.db.session import engine
    from app.services.max_bot import start_max_bot_thread
    import uvicorn
    Base.metadata.create_all(engine)
    start_max_bot_thread()
    uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('LOCAL_PORT','3002')))
