import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1","true","yes","on"}

def validated_network(mode):
    value = str(mode).strip().upper()
    if value not in {"MAINNET", "TESTNET"}:
        raise ValueError("HL_MODE must be MAINNET or TESTNET; PAPER is an execution mode, not a network")
    return value

@dataclass(frozen=True)
class Settings:
    telegram_api_id:int
    telegram_api_hash:str
    telegram_bot_token:str
    telegram_owner_id:int
    hl_mode:str
    auto_trading:bool
    master_key:str
    max_leverage:int
    max_position_pct:float
    max_total_exposure_usd:float
    max_slippage_pct:float
    entry_price_tolerance_pct:float
    watch_interval:float

def load():
    return Settings(
        int(os.getenv("TELEGRAM_API_ID","0") or 0),
        os.getenv("TELEGRAM_API_HASH","").strip(),
        os.getenv("TELEGRAM_BOT_TOKEN","").strip(),
        int(os.getenv("TELEGRAM_OWNER_ID","0") or 0),
        validated_network(os.getenv("HL_MODE","MAINNET")),
        _bool("AUTO_TRADING",False),
        os.getenv("MASTER_KEY","").strip(),
        max(1, int(os.getenv("MAX_LEVERAGE","20") or 20)),
        min(100.0, max(0.0, float(os.getenv("MAX_POSITION_PCT","100") or 100))),
        max(0.0, float(os.getenv("MAX_TOTAL_EXPOSURE_USD","25000") or 25000)),
        min(10.0, max(0.01, float(os.getenv("MAX_SLIPPAGE_PCT","0.5") or 0.5))),
        min(20.0, max(0.0, float(os.getenv("ENTRY_PRICE_TOLERANCE_PCT","0.5") or 0.5))),
        max(1.0,float(os.getenv("WATCH_INTERVAL","3") or 3)),
    )
