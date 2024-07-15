import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Streamer, User
from app.twitch_api import init_twitch_api
import redis
from sqlalchemy.exc import SQLAlchemyError
import pika
from twitchAPI.twitch import Twitch

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

main = Blueprint('main', __name__)

@main.route('/')
def index():
    logger.debug("Accessing index route")
    if not User.query.first():
        logger.debug("No users found, redirecting to setup")
        return redirect(url_for('auth.setup'))
    if not current_user.is_authenticated:
        logger.debug("User not authenticated, redirecting to login")
        return redirect(url_for('auth.login'))
    logger.debug("Rendering index template")
    return render_template('index.html', title='Home')

@main.route('/dashboard')
@login_required
def dashboard():
    logger.debug("Accessing dashboard route")
    streamers = Streamer.query.filter_by(user_id=current_user.id).all()
    logger.debug(f"Found {len(streamers)} streamers for user {current_user.id}")
    return render_template('dashboard.html', title='Dashboard', streamers=streamers)

@main.route('/ping')
def ping():
    return 'pong', 200


@main.route('/health')
def health_check():
    logger.info("Health check requested")
    health_status = {
        "status": "healthy",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "rabbitmq": "ok",
            "twitch_api": "ok"
        }
    }

    # Check database connection
    try:
        logger.info("Checking database connection")
        db.session.execute('SELECT 1')
        logger.info("Database connection successful")
    except SQLAlchemyError as e:
        logger.error(f"Database health check failed: {str(e)}")
        health_status["checks"]["database"] = "error"
        health_status["status"] = "degraded"

    # Check Redis connection
    try:
        logger.info("Checking Redis connection")
        redis_client = redis.from_url(current_app.config['CELERY_RESULT_BACKEND'])
        redis_client.ping()
        logger.info("Redis connection successful")
    except redis.RedisError as e:
        logger.error(f"Redis health check failed: {str(e)}")
        health_status["checks"]["redis"] = "error"
        health_status["status"] = "degraded"

    # Check RabbitMQ connection
    try:
        logger.info("Checking RabbitMQ connection")
        params = pika.URLParameters(current_app.config['CELERY_BROKER_URL'])
        connection = pika.BlockingConnection(params)
        connection.close()
        logger.info("RabbitMQ connection successful")
    except Exception as e:
        logger.error(f"RabbitMQ health check failed: {str(e)}")
        health_status["checks"]["rabbitmq"] = "error"
        health_status["status"] = "degraded"

    # Check Twitch API connection
    twitch = current_app.config.get('TWITCH_API')
    if twitch is None:
        logger.error("Twitch API not initialized")
        health_status["checks"]["twitch_api"] = "error"
        health_status["status"] = "degraded"
    else:
        try:
            logger.info("Checking Twitch API connection")
            # Perform a simple API call to verify the connection
            twitch.get_users(logins=['twitch'])
            logger.info("Twitch API connection successful")
        except Exception as e:
            logger.error(f"Twitch API health check failed: {str(e)}")
            health_status["checks"]["twitch_api"] = "error"
            health_status["status"] = "degraded"

    if health_status["status"] == "healthy":
        logger.info("Health check passed")
        return jsonify(health_status), 200
    else:
        logger.warning(f"Health check degraded: {health_status}")
        return jsonify(health_status), 200  # Return 200 even if degraded



# Add this to initialize the Blueprint
def init_app(app):
    logger.debug("Initializing main Blueprint")
    app.register_blueprint(main)
    logger.debug("Main Blueprint registered")