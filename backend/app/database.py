import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .utils.paths import data_dir

DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(data_dir() / 'app.db').as_posix()}"
_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if _sqlite else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if _sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
