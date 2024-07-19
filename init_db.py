import logging
import os
import sys
import time
from sqlalchemy.exc import SQLAlchemyError
from app import db, create_app
from app.models import User
import flask_migrate

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def init_db(timeout=300):  # 5 minutes timeout
    start_time = time.time()
    app = create_app()
    with app.app_context():
        try:
            logger.info("Starting database initialization")
            db.engine.connect()
            logger.info("Database connection successful")

            if not os.path.exists(os.path.join(app.root_path, 'migrations')):
                logger.info("Migrations directory not found, initializing migrations")
                flask_migrate.init()
                logger.info("Migration initialization complete")
                flask_migrate.stamp()
                logger.info("Database stamped")
                flask_migrate.migrate()
                logger.info("Migration script generated")
                flask_migrate.upgrade()
                logger.info("Database upgraded")
            else:
                logger.info("Migrations directory found, checking for pending migrations")
                flask_migrate.migrate()
                logger.info("Applying any pending migrations")
                flask_migrate.upgrade()
                logger.info("All migrations applied")

            logger.info("Creating all tables")
            db.create_all()
            logger.info("All tables created")

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
        finally:
            elapsed_time = time.time() - start_time
            if elapsed_time > timeout:
                logger.warning(f"Database initialization exceeded timeout of {timeout} seconds")
            else:
                logger.info(f"Database initialization completed in {elapsed_time:.2f} seconds")

if __name__ == '__main__':
    init_db()
    logger.info("Database initialization script completed. Exiting init_db.py")
    sys.exit(0)
