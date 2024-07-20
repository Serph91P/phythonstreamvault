import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import db, User
from config import Config

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def init_db():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    db.metadata.create_all(engine)
    
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        logger.info("Checking for existing users...")
        if not session.query(User).first():
            logger.info("No users found. The setup page will be available.")
        else:
            logger.info("Users found in the database.")
        logger.info("Database initialization complete")
    except Exception as e:
        logger.error(f"Error during database initialization: {str(e)}")
        raise
    finally:
        session.close()

if __name__ == '__main__':
    init_db()
