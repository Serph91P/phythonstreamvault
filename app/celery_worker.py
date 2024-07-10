from app import create_app
from app.celery import make_celery

app = create_app()
celery = make_celery(app)
app.app_context().push()