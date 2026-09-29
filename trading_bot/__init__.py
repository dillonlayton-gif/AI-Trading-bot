"""Paper-only trading tools with separate paper and market-data entry points.

The root performs no eager imports. Paper types live in ``trading_bot.core``;
market-data exports live in ``trading_bot.market_data``. Keeping these imports
in their own packages lets both CLIs start independently.
"""
