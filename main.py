# TOKEN = "8675708509:AAGS5VnUtGdGmBkyQbfwUJue_xodKaO1Lq4"
# CHAT_ID = "7352433831"
# SYMBOL = 'SOL/USDT'
# channel id -1003802094226
import os
import logging
import asyncio
import ccxt
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
CHANNEL_ID = os.getenv('CHANNEL_ID')

# --- 2. TRADING PARAMETERS ---
SYMBOL = 'SOL/USDT'
PORTFOLIO_SIZE = 10000.0
RISK_AMOUNT = 20.0  # Risk $20 per trade (20 basis points of $10k)
LEVERAGE = 10
RR_RATIO = 1.0

# --- 3. STATE MANAGEMENT ---
active_trade = None
trade_history = []

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


# --- 4. MATH & STRATEGY FUNCTIONS ---

def fetch_data():
    try:
        exchange = ccxt.binance()
        bars = exchange.fetch_ohlcv(SYMBOL, timeframe='15m', limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return None


def calculate_indicators(df):
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    recent_df = df.tail(30)
    resistance = recent_df['high'].max()
    support = recent_df['low'].min()
    return df, support, resistance


def get_signal(df, support, resistance):
    current_price = df['close'].iloc[-1]
    rsi = df['RSI'].iloc[-1]
    ema = df['EMA_20'].iloc[-1]

    if (current_price <= support * 1.002 and rsi < 45) or (current_price > resistance and current_price > ema):
        return 'Long'
    elif (current_price >= resistance * 0.998 and rsi > 55) or (current_price < support and current_price < ema):
        return 'Short'
    return None


def calculate_position_size(side, entry_price, support, resistance):
    if side == 'Long':
        sl = support * 0.996
        price_diff = entry_price - sl
        tp = entry_price + price_diff
    else:
        sl = resistance * 1.004
        price_diff = sl - entry_price
        tp = entry_price - price_diff

    if price_diff <= 0: price_diff = entry_price * 0.005
    units = RISK_AMOUNT / price_diff
    margin_req = (units * entry_price) / LEVERAGE
    pos_size_pct = (margin_req / PORTFOLIO_SIZE) * 100
    return round(sl, 3), round(tp, 3), round(units, 2), round(pos_size_pct, 2)


# --- 5. BROADCAST FUNCTION ---

async def broadcast_message(context, message):
    """Helper to send message to both the user and the channel."""
    # Send to Private Chat
    await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
    # Send to Channel
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Could not post to channel. Ensure bot is Admin. Error: {e}")


# --- 6. TELEGRAM COMMAND HANDLERS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 **SOL Trading Signal Bot**\n\n"
        "/checktrade - Status of the active trade\n"
        "/marketvalue - Current $SOL price\n"
        "/history - Results of last 10 trades\n"
        "/starttrade - Manually trigger signal scan"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


async def market_value_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exchange = ccxt.binance()
    ticker = exchange.fetch_ticker(SYMBOL)
    await update.message.reply_text(f"🪙 Current $SOL Value: ${ticker['last']} USDT")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("No trade history recorded yet.")
    else:
        history_str = "\n".join(trade_history[-10:])
        await update.message.reply_text(f"📜 **Last 10 Trades:**\n{history_str}", parse_mode='Markdown')


async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade
    if not active_trade:
        await update.message.reply_text("No positions currently open.")
        return
    exchange = ccxt.binance()
    curr_price = exchange.fetch_ticker(SYMBOL)['last']
    raw_move = (curr_price - active_trade['entry']) / active_trade['entry'] if active_trade['side'] == 'Long' else (
                                                                                                                               active_trade[
                                                                                                                                   'entry'] - curr_price) / \
                                                                                                                   active_trade[
                                                                                                                       'entry']
    pnl_leveraged = raw_move * LEVERAGE * 100
    msg = (
        f"$SOL {active_trade['side']} {'📈' if active_trade['side'] == 'Long' else '📉'}\n"
        f"Leverage: {LEVERAGE}x 💸\n"
        f"Entry price: ${active_trade['entry']} 🪙\n"
        f"Current Price: ${curr_price} 💵\n"
        f"PnL: {pnl_leveraged:+.2f}% 📊"
    )
    await update.message.reply_text(msg)


# --- 7. CORE MONITORING LOOP ---

async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    global active_trade, trade_history
    df = fetch_data()
    if df is None: return
    df, support, resistance = calculate_indicators(df)
    current_price = df['close'].iloc[-1]

    # A. Check if current trade should be closed
    if active_trade:
        hit_tp = (active_trade['side'] == 'Long' and current_price >= active_trade['tp']) or \
                 (active_trade['side'] == 'Short' and current_price <= active_trade['tp'])
        hit_sl = (active_trade['side'] == 'Long' and current_price <= active_trade['sl']) or \
                 (active_trade['side'] == 'Short' and current_price >= active_trade['sl'])

        if hit_tp or hit_sl:
            status = "WIN ✅" if hit_tp else "LOSS ❌"
            log_msg = f"{active_trade['side']} @ {active_trade['entry']}: {status}"
            trade_history.append(log_msg)

            close_msg = f"🚨 **TRADE CLOSED**\nResult: {status}\nExit Price: ${current_price}"
            await broadcast_message(context, close_msg)
            active_trade = None

    # B. Check for new signals
    if not active_trade:
        signal = get_signal(df, support, resistance)
        if signal:
            sl, tp, units, pos_pct = calculate_position_size(signal, current_price, support, resistance)
            active_trade = {'side': signal, 'entry': current_price, 'sl': sl, 'tp': tp, 'units': units}

            sig_msg = (
                f"🚀 **NEW SIGNAL: $SOL {signal.upper()}**\n"
                f"Entry price: ${current_price}\n"
                f"Leverage: {LEVERAGE}x\n"
                f"SL: ${sl}\n"
                f"TP: ${tp}\n"
                f"Position size: {pos_pct}%"
            )
            await broadcast_message(context, sig_msg)


async def manual_start_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if active_trade:
        await update.message.reply_text("❌ Position already open.")
    else:
        await update.message.reply_text("Scanning market...")
        await run_scan(context)


# --- 8. MAIN STARTUP ---

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.job_queue.run_repeating(run_scan, interval=60, first=5)

    app.add_handler(CommandHandler('start', help_command))
    app.add_handler(CommandHandler('marketvalue', market_value_command))
    app.add_handler(CommandHandler('checktrade', check_trade_command))
    app.add_handler(CommandHandler('history', history_command))
    app.add_handler(CommandHandler('starttrade', manual_start_trade))

    print("Bot is alive and broadcasting to channel...")
    app.run_polling()