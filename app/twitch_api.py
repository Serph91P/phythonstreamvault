import asyncio
import traceback
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from concurrent.futures import ThreadPoolExecutor

twitch = None
eventsub = None

def print_config(app):
    print("Printing configuration values:")
    print(f"TWITCH_CLIENT_ID: {'Set' if app.config.get('TWITCH_CLIENT_ID') else 'Not Set'}")
    print(f"TWITCH_CLIENT_SECRET: {'Set' if app.config.get('TWITCH_CLIENT_SECRET') else 'Not Set'}")
    print(f"TWITCH_WEBHOOK_SECRET: {'Set' if app.config.get('TWITCH_WEBHOOK_SECRET') else 'Not Set'}")
    print(f"BASE_URL: {app.config.get('BASE_URL', 'Not Set')}")
    print(f"CALLBACK_URL: {app.config.get('CALLBACK_URL', 'Not Set')}")
    print(f"EVENTSUB_WEBHOOK_PORT: {app.config.get('EVENTSUB_WEBHOOK_PORT', 'Not Set')}")

async def setup_twitch(app):
    global twitch
    print("Entering setup_twitch")
    try:
        print_config(app)
        client_id = app.config.get('TWITCH_CLIENT_ID')
        client_secret = app.config.get('TWITCH_CLIENT_SECRET')
        if not client_id or not client_secret:
            print("TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET is missing")
            return None
        print(f"Initializing Twitch API with CLIENT_ID: {client_id}")
        twitch = await Twitch(client_id, client_secret)
        await twitch.authenticate_app([])
        print("Twitch API authenticated successfully")
        return twitch
    except Exception as e:
        print(f"Failed to initialize Twitch API: {str(e)}")
        print(traceback.format_exc())
        return None

async def setup_eventsub(app, twitch_instance):
    global eventsub
    print("Entering setup_eventsub")
    try:
        if not twitch_instance:
            print("Twitch instance is None, cannot setup EventSub")
            return None

        callback_url = app.config.get('CALLBACK_URL')
        port = app.config.get('EVENTSUB_WEBHOOK_PORT')
        secret = app.config.get('TWITCH_WEBHOOK_SECRET')
        
        if not callback_url or not port or not secret:
            print(f"Missing configuration. CALLBACK_URL: {callback_url}, PORT: {port}, SECRET: {'Set' if secret else 'Not Set'}")
            return None

        print(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}, PORT: {port}")
        eventsub = EventSubWebhook(callback_url, port, secret, twitch_instance)
        print("EventSubWebhook instance created successfully")
        return eventsub
    except Exception as e:
        print(f"Failed to create EventSubWebhook instance: {str(e)}")
        print(traceback.format_exc())
        return None

async def start_eventsub(app):
    global twitch, eventsub
    print("Entering start_eventsub")
    try:
        twitch = await setup_twitch(app)
        if twitch is None:
            print("Failed to initialize Twitch API, cannot proceed with EventSub setup")
            return False

        eventsub = await setup_eventsub(app, twitch)
        if eventsub is None:
            print("Failed to initialize EventSub, cannot start EventSubWebhook")
            return False

        print(f"EventSubWebhook instance before start: {eventsub}")
        print(f"EventSubWebhook methods before start: {dir(eventsub)}")
        print("Starting EventSubWebhook")

        async def run_eventsub():
            with ThreadPoolExecutor() as executor:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(executor, eventsub.start)

        await run_eventsub()
        print("EventSubWebhook started successfully")
        return True
    except Exception as e:
        print(f"Failed to start EventSubWebhook: {str(e)}")
        print(traceback.format_exc())
        return False

def async_init(app):
    print("Starting async_init")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        print_config(app)
        success = loop.run_until_complete(start_eventsub(app))
        if success:
            print("EventSub initialized and started successfully")
        else:
            print("Failed to initialize and start EventSub")
    except Exception as e:
        print(f"Error in async_init: {str(e)}")
        print(traceback.format_exc())
    finally:
        loop.close()
    print("Finished async_init")