# Procfile — Railway auto-detects this for multi-process deployment
# One process per bot, all in the same service

crown: python3 bots/crown/bot.py
feed: python3 bots/feed/bot.py
grind: python3 bots/grind/bot.py
api: python3 -m gunicorn api_server:app -b 0.0.0.0:$PORT