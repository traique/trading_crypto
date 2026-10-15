"""The always-on app for a single host (Botkeep): web page + Telegram bot + daily trading run.

One process serves everything: the paper books (and signals for a real account)
are brought up to date after each daily candle closes (00:00 UTC, 07:00 Vietnam
time), and AI analyses run one at a time from a small queue. The bot never
places exchange orders and never asks for exchange API keys.

Start it with ``python main_botkeep.py``; configuration is environment only
(see ``deploy/botkeep/.env.example``).
"""
