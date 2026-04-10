import json
import logging
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["SPORTSGAMEODDS_API_KEY"]
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

LEAGUE_IDS = [x.strip() for x in os.environ.get(
    "LEAGUE_IDS",
    "NBA,MLS,PREMIER_LEAGUE,CHAMPIONS_LEAGUE,BUNDESLIGA,LA_LIGA,SERIE_A,LIGUE_1,EFL_CHAMPIONSHIP",
).split(",") if x.strip()]

BOOKMAKER_IDS = [x.strip().lower() for x in os.environ.get(
    "BOOKMAKER_IDS",
    "bet365,draftkings,fanduel,betmgm,betrivers,pinnacle,caesars,espnbet,fliff,bovada"
).split(",") if x.strip()]

AUTO_SCAN_MINUTES = int(os.environ.get("AUTO_SCAN_MINUTES", 15))
PRESTART_MINUTES_LIMIT = int(os.environ.get("PRESTART_MINUTES_LIMIT", 1440))
MIN_EDGE_PERCENT = float(os.environ.get("MIN_EDGE_PERCENT", 2.5))
FALLBACK_ENABLED = os.environ.get("FALLBACK_ENABLED", "true").lower() == "true"
FALLBACK_MIN_EDGE_PERCENT = float(os.environ.get("FALLBACK_MIN_EDGE_PERCENT", 0.75))
MAX_PICKS_PER_SCAN = int(os.environ.get("MAX_PICKS_PER_SCAN", 2))
DAILY_MAX_BETS = int(os.environ.get("DAILY_MAX_BETS", 4))
TIMEZONE = os.environ.get("TIMEZONE", "America/Toronto")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 25))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

API_URL = "https://api.sportsgameodds.com/v2/events"
LOCAL_TZ = ZoneInfo(TIMEZONE)
UTC = timezone.utc

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("dxm_sgo_bot")


# =========================
# STATE
# =========================
def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def default_state() -> Dict[str, Any]:
    admin_chat_ids = sorted(ADMIN_IDS) if ADMIN_IDS else []
    return {
        "daily": {"date": today_str(), "count": 0},
        "next_bet_id": 1,
        "sent_keys": [],
        "open_bets": [],
        "settled_bets": [],
        "target_chat_ids": admin_chat_ids,
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        state = default_state()
        save_state(state)
        return state
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        log.exception("Failed to load state, recreating fresh state")
        state = default_state()
        save_state(state)
        return state


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def reset_daily_if_needed(state: Dict[str, Any]) -> None:
    if state["daily"].get("date") != today_str():
        state["daily"] = {"date": today_str(), "count": 0}
        save_state(state)


# =========================
# UTILS
# =========================
def american_to_decimal(american: str) -> Optional[float]:
    try:
        a = int(str(american).strip())
    except Exception:
        return None
    if a > 0:
        return 1 + (a / 100)
    if a < 0:
        return 1 + (100 / abs(a))
    return None


def american_to_implied_prob(american: str) -> Optional[float]:
    try:
        a = int(str(american).strip())
    except Exception:
        return None
    if a > 0:
        return 100 / (a + 100)
    if a < 0:
        return abs(a) / (abs(a) + 100)
    return None


def ev_percent(fair_american: str, offered_american: str) -> Optional[float]:
    fair_prob = american_to_implied_prob(fair_american)
    offered_dec = american_to_decimal(offered_american)
    if fair_prob is None or offered_dec is None:
        return None
    return ((fair_prob * offered_dec) - 1.0) * 100.0


def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def fmt_local_time(dt_str: str) -> str:
    dt = parse_iso(dt_str)
    return dt.strftime("%b %d, %I:%M %p %Z")


def chunks(seq: List[Any], size: int) -> List[List[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def clean_market_name(odd: Dict[str, Any], event: Dict[str, Any]) -> str:
    bet_type = odd.get("betTypeID")
    side = odd.get("sideID", "").upper()
    stat_id = odd.get("statID")
    period = odd.get("periodID")
    line = None
    if bet_type == "sp":
        line = odd.get("bookSpread") or odd.get("fairSpread")
    elif bet_type == "ou":
        line = odd.get("bookOverUnder") or odd.get("fairOverUnder")

    team_home = event["teams"]["home"]["names"]["medium"]
    team_away = event["teams"]["away"]["names"]["medium"]
    stat_entity = odd.get("statEntityID")

    if bet_type == "ml":
        pick_name = team_home if stat_entity == "home" else team_away if stat_entity == "away" else side.title()
        return f"Moneyline • {pick_name}"

    if bet_type == "sp":
        pick_name = team_home if stat_entity == "home" else team_away if stat_entity == "away" else side.title()
        return f"Spread • {pick_name} {line}"

    if bet_type == "ou":
        if stat_id == "points":
            return f"Total • {side.title()} {line}"
        label = odd.get("marketName") or stat_id or "Over/Under"
        return f"{label} • {side.title()} {line}"

    return odd.get("marketName") or odd.get("oddID")


def is_supported_odd(odd: Dict[str, Any]) -> bool:
    if odd.get("playerID"):
        return False
    if odd.get("started") or odd.get("ended") or odd.get("cancelled"):
        return False
    if not odd.get("bookOddsAvailable") or not odd.get("fairOddsAvailable"):
        return False

    stat_id = odd.get("statID")
    bet_type = odd.get("betTypeID")
    period = odd.get("periodID")

    if period not in {"game", "reg"}:
        return False

    # Keep this version focused on core markets only.
    if bet_type == "ml":
        return True
    if bet_type == "sp" and stat_id == "points":
        return True
    if bet_type == "ou" and stat_id == "points":
        return True
    return False


def best_book_for_odd(odd: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any], float]]:
    by_book = odd.get("byBookmaker") or {}
    fair_odds = odd.get("fairOdds")
    target_spread = str(odd.get("bookSpread") or odd.get("fairSpread") or "")
    target_total = str(odd.get("bookOverUnder") or odd.get("fairOverUnder") or "")

    candidates = []
    for book_id, data in by_book.items():
        if BOOKMAKER_IDS and book_id.lower() not in BOOKMAKER_IDS:
            continue
        if not data.get("available"):
            continue
        offered_odds = data.get("odds")
        if not offered_odds:
            continue

        bet_type = odd.get("betTypeID")
        if bet_type == "sp":
            if str(data.get("spread", "")) != target_spread:
                continue
        elif bet_type == "ou":
            if str(data.get("overUnder", "")) != target_total:
                continue

        edge = ev_percent(fair_odds, offered_odds)
        if edge is None:
            continue
        candidates.append((book_id, data, edge))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0]


@dataclass
class Candidate:
    event_id: str
    league_id: str
    starts_at: str
    matchup: str
    odd_id: str
    opposing_odd_id: Optional[str]
    market_label: str
    fair_odds: str
    book_odds: str
    book_name: str
    edge_percent: float
    deeplink: Optional[str]
    sport_id: str

    def dedupe_key(self) -> str:
        return f"{self.event_id}"

def fetch_events() -> List[Dict[str, Any]]:
    starts_after = datetime.now(UTC) - timedelta(minutes=3)
    starts_before = datetime.now(UTC) + timedelta(minutes=PRESTART_MINUTES_LIMIT)

    params = {
        "apiKey": API_KEY,
        "leagueID": ",".join(LEAGUE_IDS),
        "oddsAvailable": "true",
        "includeAltLines": "false",
        "started": "false",
        "live": "false",
        "ended": "false",
        "finalized": "false",
        "startsAfter": starts_after.isoformat().replace("+00:00", "Z"),
        "startsBefore": starts_before.isoformat().replace("+00:00", "Z"),
        "limit": 100,
    }

    all_events: List[Dict[str, Any]] = []
    cursor = None

    while True:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        resp = requests.get(API_URL, params=q, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"SportsGameOdds returned success=false: {payload}")
        data = payload.get("data", [])
        all_events.extend(data)
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    return all_events


def build_candidates(events: List[Dict[str, Any]]) -> List[Candidate]:
    out: List[Candidate] = []
    for event in events:
        if event.get("type") != "match":
            continue
        status = event.get("status") or {}
        if status.get("started") or status.get("ended") or status.get("cancelled") or status.get("live"):
            continue
        odds = event.get("odds") or {}
        if not odds:
            continue

        event_candidates: List[Candidate] = []
        home = event["teams"]["home"]["names"]["medium"]
        away = event["teams"]["away"]["names"]["medium"]
        matchup = f"{away} @ {home}"

        for odd in odds.values():
            if not is_supported_odd(odd):
                continue
            best = best_book_for_odd(odd)
            if not best:
                continue
            book_name, book_data, edge = best
            event_candidates.append(
                Candidate(
                    event_id=event["eventID"],
                    league_id=event["leagueID"],
                    sport_id=event["sportID"],
                    starts_at=status["startsAt"],
                    matchup=matchup,
                    odd_id=odd["oddID"],
                    opposing_odd_id=odd.get("opposingOddID"),
                    market_label=clean_market_name(odd, event),
                    fair_odds=str(odd.get("fairOdds")),
                    book_odds=str(book_data.get("odds")),
                    book_name=book_name,
                    edge_percent=edge,
                    deeplink=book_data.get("deeplink"),
                )
            )

        if not event_candidates:
            continue

        event_candidates.sort(key=lambda c: c.edge_percent, reverse=True)
        out.append(event_candidates[0])  # one pick per game max

    out.sort(key=lambda c: c.edge_percent, reverse=True)
    return out


def select_picks(candidates: List[Candidate], state: Dict[str, Any]) -> List[Candidate]:
    reset_daily_if_needed(state)
    sent_keys = set(state.get("sent_keys", []))
    remaining_for_day = max(0, DAILY_MAX_BETS - state["daily"].get("count", 0))
    if remaining_for_day <= 0:
        return []

    fresh = [c for c in candidates if c.dedupe_key() not in sent_keys]

    # 1) safer odds only: -150 to +300
    filtered = []
    for c in fresh:
        try:
            american_odds = int(str(c.book_odds).strip())
        except Exception:
            continue

        if american_odds < -150 or american_odds > 300:
            continue

        # 2) stronger edge only: 10%+
        if c.edge_percent < 20:
            continue

        filtered.append(c)

    # 3) only one bet per game
    one_per_event = []
    seen_events = set()
    for c in filtered:
        if c.event_id in seen_events:
            continue
        seen_events.add(c.event_id)
        one_per_event.append(c)

    # 4) best bets first
    one_per_event.sort(key=lambda x: x.edge_percent, reverse=True)

    selected = one_per_event[: min(MAX_PICKS_PER_SCAN, remaining_for_day)]

    return selected


def register_sent_pick(state: Dict[str, Any], pick: Candidate) -> Dict[str, Any]:
    bet_id = state["next_bet_id"]
    state["next_bet_id"] += 1
    state["daily"]["count"] += 1
    state.setdefault("sent_keys", []).append(pick.dedupe_key())
    state.setdefault("open_bets", []).append(
        {
            "bet_id": bet_id,
            "status": "open",
            "sent_at": now_local().isoformat(),
            **asdict(pick),
        }
    )
    save_state(state)
    return state["open_bets"][-1]


def settle_bet(state: Dict[str, Any], bet_id: int, result: str) -> Optional[Dict[str, Any]]:
    result = result.lower().strip()
    if result not in {"win", "loss", "push"}:
        return None
    for bet in state.get("open_bets", []):
        if bet["bet_id"] == bet_id and bet["status"] == "open":
            bet["status"] = result
            bet["settled_at"] = now_local().isoformat()
            state.setdefault("settled_bets", []).append(dict(bet))
            state["open_bets"] = [b for b in state["open_bets"] if not (b["bet_id"] == bet_id and b["status"] != "open")]
            save_state(state)
            return bet
    return None


def stats_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    settled = state.get("settled_bets", [])
    wins = sum(1 for b in settled if b["status"] == "win")
    losses = sum(1 for b in settled if b["status"] == "loss")
    pushes = sum(1 for b in settled if b["status"] == "push")
    graded = wins + losses
    win_rate = (wins / graded * 100.0) if graded else 0.0
    open_count = len(state.get("open_bets", []))
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "graded": graded,
        "open": open_count,
        "win_rate": win_rate,
        "today_count": state.get("daily", {}).get("count", 0),
    }


def format_pick_message(bet: Dict[str, Any]) -> str:
    lines = [
        "🔥 <b>DXM VIP VALUE PLAY</b>",
        f"🏟️ <b>{bet['league_id']}</b>",
        f"📅 {fmt_local_time(bet['starts_at'])}",
        f"🎯 <b>{bet['matchup']}</b>",
        "",
        f"✅ <b>Pick:</b> {bet['market_label']}",
        f"🏪 <b>Book:</b> {bet['book_name']}",
        f"💵 <b>Odds:</b> {bet['book_odds']}",
        f"📈 <b>Fair:</b> {bet['fair_odds']}",
        f"⚡ <b>Edge:</b> {bet['edge_percent']:.2f}%",
        f"🆔 <b>Bet ID:</b> {bet['bet_id']}",
    ]
    if bet.get("deeplink"):
        lines.append(f"🔗 {bet['deeplink']}")
    lines.append("")
    lines.append("Risk smart. One play per game. No chasing.")
    return "\n".join(lines)


def format_open_bets(state: Dict[str, Any]) -> str:
    open_bets = state.get("open_bets", [])
    if not open_bets:
        return "No open bets right now."
    rows = ["<b>OPEN BETS</b>", ""]
    for bet in open_bets[:20]:
        rows.append(
            f"#{bet['bet_id']} • {bet['league_id']} • {bet['matchup']}\n"
            f"{bet['market_label']} @ {bet['book_name']} {bet['book_odds']}\n"
            f"Starts: {fmt_local_time(bet['starts_at'])}"
        )
        rows.append("")
    return "\n".join(rows).strip()


def format_stats(state: Dict[str, Any]) -> str:
    s = stats_summary(state)
    return (
        "📊 <b>DXM STATS</b>\n"
        f"Today sent: <b>{s['today_count']}</b>\n"
        f"Open: <b>{s['open']}</b>\n"
        f"Wins: <b>{s['wins']}</b>\n"
        f"Losses: <b>{s['losses']}</b>\n"
        f"Pushes: <b>{s['pushes']}</b>\n"
        f"Win rate: <b>{s['win_rate']:.1f}%</b>"
    )


def ensure_target_chat(state: Dict[str, Any], chat_id: int) -> None:
    state.setdefault("target_chat_ids", [])
    if chat_id not in state["target_chat_ids"]:
        state["target_chat_ids"].append(chat_id)
        save_state(state)


def user_is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS) if ADMIN_IDS else True


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    ensure_target_chat(state, update.effective_chat.id)
    text = (
        "DXM SportsGameOdds bot is live.\n\n"
        "Commands:\n"
        "/scan - scan now\n"
        "/openbets - show open bets\n"
        "/stats - show record\n"
        "/win <bet_id> - mark win\n"
        "/loss <bet_id> - mark loss\n"
        "/push <bet_id> - mark push\n"
        "/config - show current settings"
    )
    await update.message.reply_text(text)


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_is_admin(update):
        return
    text = (
        "<b>DXM CONFIG</b>\n"
        f"Leagues: <code>{', '.join(LEAGUE_IDS)}</code>\n"
        f"Books: <code>{', '.join(BOOKMAKER_IDS)}</code>\n"
        f"Auto scan: <b>{AUTO_SCAN_MINUTES} min</b>\n"
        f"Prestart window: <b>{PRESTART_MINUTES_LIMIT} min</b>\n"
        f"Min edge: <b>{MIN_EDGE_PERCENT}%</b>\n"
        f"Fallback: <b>{FALLBACK_ENABLED}</b> ({FALLBACK_MIN_EDGE_PERCENT}%)\n"
        f"Max per scan: <b>{MAX_PICKS_PER_SCAN}</b>\n"
        f"Daily max: <b>{DAILY_MAX_BETS}</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_is_admin(update):
        return
    await run_scan_and_send(context, only_chat_id=update.effective_chat.id, manual=True)


async def openbets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    await update.message.reply_text(format_open_bets(state), parse_mode=ParseMode.HTML)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    await update.message.reply_text(format_stats(state), parse_mode=ParseMode.HTML)


async def settle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, result: str) -> None:
    if not user_is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(f"Use /{result} <bet_id>")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Bet ID has to be a number.")
        return

    state = load_state()
    bet = settle_bet(state, bet_id, result)
    if not bet:
        await update.message.reply_text("Could not find an open bet with that ID.")
        return
    await update.message.reply_text(
        f"Marked bet #{bet_id} as {result.upper()}.\n\n{format_stats(state)}",
        parse_mode=ParseMode.HTML,
    )


async def win_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await settle_cmd(update, context, "win")


async def loss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await settle_cmd(update, context, "loss")


async def push_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await settle_cmd(update, context, "push")


# =========================
# SCAN LOGIC
# =========================
async def run_scan_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    only_chat_id: Optional[int] = None,
    manual: bool = False,
) -> None:
    state = load_state()
    reset_daily_if_needed(state)

    try:
        events = fetch_events()
        candidates = build_candidates(events)
        picks = select_picks(candidates, state)
    except Exception as exc:
        log.exception("Scan failed")
        if manual and only_chat_id is not None:
            await context.bot.send_message(chat_id=only_chat_id, text=f"Scan failed: {exc}")
        return

    target_chat_ids = [only_chat_id] if only_chat_id is not None else list(state.get("target_chat_ids", []))
    target_chat_ids = [c for c in target_chat_ids if c is not None]

    if not picks:
        if manual and only_chat_id is not None:
            await context.bot.send_message(
                chat_id=only_chat_id,
                text=(
                    "No fresh bets found right now.\n\n"
                    f"Checked leagues: {', '.join(LEAGUE_IDS)}\n"
                    f"Min edge: {MIN_EDGE_PERCENT}%"
                ),
            )
        return

    for pick in picks:
        bet = register_sent_pick(state, pick)
        message = format_pick_message(bet)
        for chat_id in target_chat_ids:
            try:
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                log.exception("Failed sending pick to chat %s", chat_id)


async def autoscan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_scan_and_send(context)


# =========================
# MAIN
# =========================
def main() -> None:
    state = load_state()
    reset_daily_if_needed(state)
    if ADMIN_IDS and not state.get("target_chat_ids"):
        state["target_chat_ids"] = sorted(ADMIN_IDS)
        save_state(state)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", start_cmd))
    application.add_handler(CommandHandler("config", config_cmd))
    application.add_handler(CommandHandler("scan", scan_cmd))
    application.add_handler(CommandHandler("openbets", openbets_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("win", win_cmd))
    application.add_handler(CommandHandler("loss", loss_cmd))
    application.add_handler(CommandHandler("push", push_cmd))

    if application.job_queue is None:
        raise RuntimeError("Job queue is unavailable. Install python-telegram-bot[job-queue].")

    application.job_queue.run_repeating(autoscan_job, interval=AUTO_SCAN_MINUTES * 60, first=15)

    log.info("DXM SportsGameOdds bot started")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
