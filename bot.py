import logging
import sqlite3
import asyncio
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import Application, ChatMemberHandler, ContextTypes, Defaults

# --- CONFIGURATION ---
import os
TOKEN = os.getenv("BOT_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID") 

# Timezone setup
IST = pytz.timezone('Asia/Kolkata')

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- DATABASE MANAGEMENT ---
class BotState:
    # We save the DB in a folder named 'data'
def __init__(self, db_name="/app/data/bot_data.db"): 
    # Ensure the directory exists
    os.makedirs(os.path.dirname(db_name), exist_ok=True) 
    self.conn = sqlite3.connect(db_name, check_same_thread=False)
    self.create_tables()

    def create_tables(self):
        with self.conn:
            # Table to store daily counters per Channel (chat_id)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    date_str TEXT,
                    join_count INTEGER DEFAULT 0,
                    leave_count INTEGER DEFAULT 0
                )
            """)
            # Table to track WHO joined today (User + Chat combination)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS today_joiners (
                    user_id INTEGER,
                    chat_id INTEGER,
                    PRIMARY KEY (user_id, chat_id)
                )
            """)

    def check_date_reset(self):
        """Checks if the date has changed. If yes, resets EVERYTHING."""
        current_date = datetime.now(IST).strftime('%Y-%m-%d')
        
        # Check the date of the first row (assuming all rows sync to same day)
        cursor = self.conn.execute("SELECT date_str FROM daily_stats LIMIT 1")
        row = cursor.fetchone()
        
        # If DB is empty or date changed
        if not row or row[0] != current_date:
            with self.conn:
                # Reset counters for ALL channels to 0 and update date
                self.conn.execute("UPDATE daily_stats SET date_str = ?, join_count = 0, leave_count = 0", (current_date,))
                # Clear the list of who joined today
                self.conn.execute("DELETE FROM today_joiners")
            logging.info(f"Date changed to {current_date}. All counters reset.")

    def add_join(self, chat_id, chat_title, user_id):
        self.check_date_reset()
        current_date = datetime.now(IST).strftime('%Y-%m-%d')
        
        with self.conn:
            # Ensure the channel exists in our DB
            self.conn.execute("""
                INSERT OR IGNORE INTO daily_stats (chat_id, chat_title, date_str, join_count, leave_count) 
                VALUES (?, ?, ?, 0, 0)
            """, (chat_id, chat_title, current_date))
            
            # Update Title (in case it changed) and Increment Join
            self.conn.execute("UPDATE daily_stats SET chat_title = ?, join_count = join_count + 1 WHERE chat_id = ?", (chat_title, chat_id))
            
            # Record that this user joined THIS specific channel today
            self.conn.execute("INSERT OR IGNORE INTO today_joiners (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
            
            # Get the new count
            cursor = self.conn.execute("SELECT join_count FROM daily_stats WHERE chat_id = ?", (chat_id,))
            return cursor.fetchone()[0]

    def add_leave(self, chat_id, user_id):
        self.check_date_reset()
        # Check if this user joined THIS specific channel today
        cursor = self.conn.execute("SELECT 1 FROM today_joiners WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        
        if cursor.fetchone():
            with self.conn:
                self.conn.execute("UPDATE daily_stats SET leave_count = leave_count + 1 WHERE chat_id = ?", (chat_id,))
            
            # Return the new leave count
            cursor = self.conn.execute("SELECT leave_count FROM daily_stats WHERE chat_id = ?", (chat_id,))
            return cursor.fetchone()[0]
        return None

    def get_all_reports(self):
        """Returns a list of stats for all tracked channels."""
        cursor = self.conn.execute("SELECT chat_title, date_str, join_count, leave_count FROM daily_stats")
        return cursor.fetchall()

db = BotState()

# --- HANDLERS ---

async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks Joins and Leaves."""
    if not update.chat_member:
        return

    result = update.chat_member
    user = result.from_user
    chat_id = result.chat.id
    chat_title = result.chat.title
    
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    # 1. USER JOINED
    if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
        count = db.add_join(chat_id, chat_title, user.id)
        
        msg = (
            f"🟢 **New Join** | {chat_title}\n"
            f"Serial: #{count}\n"
            f"User: {user.full_name} (`{user.id}`)\n"
            f"Time: {datetime.now(IST).strftime('%I:%M %p IST')}"
        )
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=msg, parse_mode="Markdown")

    # 2. USER LEFT
    elif old_status in ["member", "administrator"] and new_status in ["left", "kicked"]:
        leave_serial = db.add_leave(chat_id, user.id)
        
        if leave_serial is not None:
            msg = (
                f"🔴 **Same Day Leave** | {chat_title}\n"
                f"Serial: #{leave_serial}\n"
                f"User: {user.full_name} (`{user.id}`)\n"
                f"Time: {datetime.now(IST).strftime('%I:%M %p IST')}"
            )
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=msg, parse_mode="Markdown")

# --- DAILY REPORT JOB ---

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Sends reports for ALL channels."""
    reports = db.get_all_reports()
    
    if not reports:
        return

    # Create a summary message or send individual messages
    for row in reports:
        chat_title, date_str, joins, leaves = row
        stay_count = joins - leaves
        
        if joins == 0 and leaves == 0:
            continue # Skip inactive channels

        report_msg = (
            f"📊 **Daily Report: {chat_title}**\n"
            f"Date: {date_str}\n"
            f"-------------------------------\n"
            f"✅ Total Joins: {joins}\n"
            f"❌ Same Day Leaves: {leaves}\n"
            f"-------------------------------\n"
            f"users Stayed: {stay_count}"
        )
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=report_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Failed to send report for {chat_title}: {e}")

    # Reset counters logic is handled automatically by 'check_date_reset' on the next activity
    # But we can force update the date now to be clean
    next_day = datetime.now(IST).strftime('%Y-%m-%d')
    with db.conn:
        db.conn.execute("UPDATE daily_stats SET date_str = ?, join_count = 0, leave_count = 0", (next_day,))
        db.conn.execute("DELETE FROM today_joiners")

# --- MAIN EXECUTION ---

def main():
    if not TOKEN or not LOG_CHANNEL_ID:
        print("Error: BOT_TOKEN or LOG_CHANNEL_ID missing.")
        return

    defaults = Defaults(tzinfo=IST)
    application = Application.builder().token(TOKEN).defaults(defaults).build()

    application.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.CHAT_MEMBER))

    job_queue = application.job_queue
    job_queue.run_daily(send_daily_report, time=time(hour=0, minute=0, second=0, tzinfo=IST))

    print("Multi-Channel Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
