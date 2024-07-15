import os
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def on_starting(server):
    server.worker_id = 0
    logger.info("Gunicorn server is starting")

def pre_request(worker, req):
    if not hasattr(worker, 'worker_id'):
        worker.worker_id = int(os.environ.get('GUNICORN_WORKER_ID', 0))
    worker.worker_id += 1
    os.environ["GUNICORN_WORKER_ID"] = str(worker.worker_id)
    logger.debug(f"Processing request with worker ID: {worker.worker_id}")

def post_worker_init(worker):
    logger.info(f"Worker initialized")

bind = "0.0.0.0:8000"
workers = 4
threads = 2
worker_class = "gthread"
timeout = 120

logger.info(f"Gunicorn configuration: bind={bind}, workers={workers}, threads={threads}, worker_class={worker_class}, timeout={timeout}")
