from app import create_app, celery
from app.twitch_api import ensure_twitch_initialized
import asyncio

app = create_app()
app.app_context().push()

@app.before_request
def log_session_info():
    logger.debug(f"Session data: {session}")
def init_twitch():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ensure_twitch_initialized(app))