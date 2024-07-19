import logging
import os
import sys
from sqlalchemy.exc import SQLAlchemyError
from app import db, create_app
from app.models import User
import flask_migrate

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def init_db():
    app = create_app()
    with app.app_context():
        try:
            # Check database connection
            db.engine.connect()
            logger.info("Database connection successful")

            if not os.path.exists(os.path.join(app.root_path, 'migrations')):
                logger.info("Migrations directory not found, initializing migrations")
                flask_migrate.init()
                flask_migrate.stamp()
                flask_migrate.migrate()
                flask_migrate.upgrade()

            logger.info("Checking for and applying any pending migrations")
            flask_migrate.upgrade()

            logger.info("Creating all tables")
            db.create_all()

            if not User.query.first():
                logger.info("No users found. The setup page will be available.")
            else:
                logger.info("Users found in the database.")

            logger.info("Database initialization complete")
        except SQLAlchemyError as e:
            logger.error(f"Database error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during database initialization: {str(e)}")
            raise

if __name__ == '__main__':
    init_db()
    logger.info("Database initialization complete. Exiting init_db.py")
    sys.exit(0)
