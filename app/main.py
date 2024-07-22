import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Streamer, User, Stream
from app.tasks import add_streamer_task, resubscribe_all_streamers
import redis
from sqlalchemy.exc import SQLAlchemyError
import pika

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

    try:
        logger.info("Checking database connection")
        db.session.execute('SELECT 1')
        logger.info("Database connection successful")
    except SQLAlchemyError as e:
        logger.error(f"Database health check failed: {str(e)}")
        health_status["checks"]["database"] = "error"
        health_status["status"] = "degraded"

    try:
        logger.info("Checking Redis connection")
        redis_client = redis.from_url(current_app.config['CELERY_RESULT_BACKEND'])
        redis_client.ping()
        logger.info("Redis connection successful")
    except redis.RedisError as e:
        logger.error(f"Redis health check failed: {str(e)}")
        health_status["checks"]["redis"] = "error"
        health_status["status"] = "degraded"

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

    twitch_api = current_app.config.get('TWITCH_API')
    if twitch_api is None:
        logger.error("Twitch API not initialized")
        health_status["checks"]["twitch_api"] = "error"
        health_status["status"] = "degraded"
    else:
        try:
            logger.info("Checking Twitch API connection")
            twitch_api.twitch.get_users(logins=['twitch'])
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
        return jsonify(health_status), 200

@main.route('/add_streamer', methods=['POST'])
@login_required
def add_streamer():
    username = request.form.get('username')
    if not username:
        return jsonify({'error': 'No username provided'}), 400

    task = add_streamer_task.delay(username, current_user.id)
    return jsonify({'task_id': str(task.id), 'status': 'Streamer is being added...'})

@main.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    task = add_streamer_task.AsyncResult(task_id)
    current_app.logger.info(f"Task status for {task_id}: {task.state}")
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Streamer is being added...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'result': task.result,
        }
    else:
        response = {
            'state': task.state,
            'status': str(task.info),
        }
    return jsonify(response)

@main.route('/manage_subscriptions')
@login_required
def manage_subscriptions_page():
    return render_template('manage_subscriptions.html', title='Manage Subscriptions')

@main.route('/api/list_subscriptions')
@login_required
def list_subscriptions():
    twitch_api = current_app.config.get('TWITCH_API')
    if not twitch_api:
        return jsonify({'error': 'Twitch API not initialized'}), 500
    subscriptions = twitch_api.get_eventsub_subscriptions()
    return jsonify(subscriptions)

@main.route('/api/delete_subscriptions', methods=['POST'])
@login_required
def delete_subscriptions():
    twitch_api = current_app.config['TWITCH_API']
    result = twitch_api.delete_all_subscriptions()
    return jsonify(result)

@main.route('/api/resubscribe_all', methods=['POST'])
@login_required
def resubscribe_all():
    task = resubscribe_all_streamers.delay()
    return jsonify({"task_id": str(task.id)})

@main.route('/api/resubscribe_status/<task_id>')
@login_required
def resubscribe_status(task_id):
    task = resubscribe_all_streamers.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Resubscribe process is still running...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'result': task.result,
        }
    else:
        response = {
            'state': task.state,
            'status': str(task.info),
        }
    return jsonify(response)

@main.app_errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404


def init_app(app):
    logger.debug("Initializing main Blueprint")
    app.register_blueprint(main)
    logger.debug("Main Blueprint registered")
