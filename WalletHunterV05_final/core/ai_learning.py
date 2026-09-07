"""Shared PUBLIC-market research learner; never a trading permission gate.

An hourly decision uses 60 closed 15m bars. After one waiting bar, a virtual
trade enters i+2 OPEN and exits i+5 CLOSE (one-hour holding period). Labels use
assumed 5bps fees and 5bps slippage on each side; funding is not included.

The bootstrap holdout/purge are sealed forever. Subsequent SGD updates use
newly matured training labels only. Frozen prospective predictions are scored
BEFORE their labels can train the next model (prequential monitoring). Bootstrap
validation stays attached to its original model, never to a later model.
"""
from contextlib import closing
import json
import math
import os
import sqlite3


BAR_MS = 900_000
HOUR_MS = 3_600_000
FEE = .0005
SLIPPAGE = .0005
FEATURES = ["return1_direction", "return4_direction", "return16_direction",
            "ema20_50_direction", "macd_hist_direction", "rsi14_direction",
            "atr14_fraction", "volume20_relative"]
MIN_TRAIN_GROUPS = 100
MIN_TEST_GROUPS = 40
MAX_SERIES = 4096
MAX_TRAIN_ROWS = 4000


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid public candle number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite public candle number")
    return result


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid public candle time")
    return value


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _loads(value):
    def invalid(_):
        raise ValueError("Invalid stored research JSON")
    return json.loads(value, parse_constant=invalid)


def _ema(values, period):
    out = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        out.append(out[-1] + alpha * (value - out[-1]))
    return out


def _features(bars):
    """Exactly 60 past/current bars; no future context or current funding/OI."""
    if len(bars) != 60 or any(b["t"] - a["t"] != BAR_MS for a, b in zip(bars, bars[1:])):
        raise ValueError("Incomplete public feature history")
    prices = [bar["close"] for bar in bars]
    differences = [b - a for a, b in zip(prices, prices[1:])]
    gain = sum(max(0, d) for d in differences[:14]) / 14
    loss = sum(max(0, -d) for d in differences[:14]) / 14
    for change in differences[14:]:
        gain = (13 * gain + max(0, change)) / 14
        loss = (13 * loss + max(0, -change)) / 14
    rsi = 1 - 1 / (1 + gain / loss) if loss else (1 if gain else .5)
    macd = [a - b for a, b in zip(_ema(prices, 12), _ema(prices, 26))]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
              for a, b in zip(bars, bars[1:])]
    atr = sum(ranges[:14]) / 14
    for value in ranges[14:]:
        atr = (13 * atr + value) / 14
    volume = sum(bar["volume"] for bar in bars[-21:-1]) / 20
    if volume <= 0:
        raise ValueError("Insufficient traded volume")
    last = prices[-1]
    result = [last / prices[-2] - 1, last / prices[-5] - 1, last / prices[-17] - 1,
              (_ema(prices, 20)[-1] - _ema(prices, 50)[-1]) / last,
              (macd[-1] - _ema(macd, 9)[-1]) / last, rsi - .5,
              atr / last, bars[-1]["volume"] / volume - 1]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Nonfinite public features")
    return result


def _direction(features, side):
    sign = 1 if side == "LONG" else -1
    return [value * sign if index < 6 else value for index, value in enumerate(features)]


def _outcome(entry, exit_price, side):
    sign = 1 if side == "LONG" else -1
    filled_entry = entry * (1 + sign * SLIPPAGE)
    filled_exit = exit_price * (1 - sign * SLIPPAGE)
    net = (sign * (filled_exit - filled_entry) - FEE * (filled_entry + filled_exit)) / filled_entry
    if not math.isfinite(net):
        raise ValueError("Invalid virtual research outcome")
    return net, int(net > 0)


def _scaler(rows):
    vectors = [_loads(row["features"]) for row in rows]
    means = [math.fsum(vector[i] for vector in vectors) / len(vectors) for i in range(8)]
    scales = [max(1e-8, math.sqrt(math.fsum((vector[i] - means[i]) ** 2 for vector in vectors) / len(vectors)))
              for i in range(8)]
    return {"mean": means, "scale": scales, "basis": "bootstrap_train_only_frozen"}


def _transform(features, scaler):
    return [max(-8., min(8., (value - mean) / scale))
            for value, mean, scale in zip(features, scaler["mean"], scaler["scale"])]


def _probability(features, weights, scaler):
    z = weights[0] + math.fsum(w * x for w, x in zip(weights[1:], _transform(features, scaler)))
    return 1 / (1 + math.exp(-max(-40., min(40., z))))


def _fit(rows, weights, scaler):
    """Bounded deterministic SGD: at most five passes over fresh labels."""
    weights = list(weights)
    prepared = [(_transform(_loads(row["features"]), scaler), row["label"]) for row in rows]
    for epoch in range(5):
        rate = .025 / (1 + epoch * .25)
        for values, label in prepared:
            z = weights[0] + math.fsum(w * x for w, x in zip(weights[1:], values))
            p = 1 / (1 + math.exp(-max(-40., min(40., z))))
            error = p - label
            weights[0] -= rate * error
            for index, value in enumerate(values, 1):
                weights[index] -= rate * (error * value + .001 * weights[index])
    if not all(math.isfinite(weight) for weight in weights):
        raise ValueError("Invalid fitted research model")
    return weights


def _metrics(rows, kind, *, model_version=None):
    result = {"kind": kind, "groups": len({row["decision_ms"] for row in rows}), "samples": len(rows),
              "brier": None, "log_loss": None, "accuracy": None, "baseline_brier": None,
              "baseline_log_loss": None, "positive_rate": None}
    if model_version is not None:
        result["model_version"] = model_version
    if rows:
        def log_loss(label, p):
            p = max(1e-12, min(1 - 1e-12, p))
            return -label * math.log(p) - (1 - label) * math.log(1 - p)
        count = len(rows)
        result.update(brier=math.fsum((row["probability"] - row["label"]) ** 2 for row in rows) / count,
                      log_loss=math.fsum(log_loss(row["label"], row["probability"]) for row in rows) / count,
                      accuracy=sum(int(row["probability"] >= .5) == row["label"] for row in rows) / count,
                      baseline_brier=math.fsum((row["baseline"] - row["label"]) ** 2 for row in rows) / count,
                      baseline_log_loss=math.fsum(log_loss(row["label"], row["baseline"]) for row in rows) / count,
                      positive_rate=sum(row["label"] for row in rows) / count)
    return result


class AiLearning:
    def __init__(self, root):
        self.path = os.path.join(root, "data", "ai_learning.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_candles(
                    coin TEXT NOT NULL,t INTEGER NOT NULL,close_ms INTEGER NOT NULL,
                    open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,
                    volume REAL NOT NULL,observed_ms INTEGER NOT NULL,PRIMARY KEY(coin,t));
                CREATE TABLE IF NOT EXISTS learning_examples(
                    coin TEXT NOT NULL,decision_ms INTEGER NOT NULL,direction TEXT NOT NULL,
                    features TEXT NOT NULL,entry_ms INTEGER NOT NULL,exit_ms INTEGER NOT NULL,
                    entry_price REAL NOT NULL,exit_price REAL NOT NULL,net_return REAL NOT NULL,label INTEGER NOT NULL,
                    partition TEXT,trained_version INTEGER,PRIMARY KEY(coin,decision_ms,direction));
                CREATE TABLE IF NOT EXISTS learning_models(
                    version INTEGER PRIMARY KEY AUTOINCREMENT,created_ms INTEGER NOT NULL,
                    trained_through_ms INTEGER NOT NULL,last_decision_ms INTEGER NOT NULL,
                    weights TEXT NOT NULL,scaler TEXT NOT NULL,train_samples INTEGER NOT NULL,
                    train_positives INTEGER NOT NULL,baseline REAL NOT NULL,evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_predictions(
                    coin TEXT NOT NULL,decision_ms INTEGER NOT NULL,direction TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,entry_ms INTEGER NOT NULL,deadline_ms INTEGER NOT NULL,
                    model_version INTEGER NOT NULL,probability REAL NOT NULL,baseline REAL NOT NULL,
                    features TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',
                    label INTEGER,net_return REAL,settled_ms INTEGER,
                    PRIMARY KEY(coin,decision_ms,direction));
                CREATE TABLE IF NOT EXISTS learning_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            db.commit()
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _meta(db, name):
        row = db.execute("SELECT value FROM learning_meta WHERE name=?", (name,)).fetchone()
        return _loads(row["value"]) if row else None

    @staticmethod
    def _set_meta(db, name, value):
        db.execute("INSERT INTO learning_meta(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   (name, _json(value)))

    def ingest(self, coin, candles, now_ms):
        if coin not in ("BTC", "ETH"):
            raise ValueError("Research uses public BTC/ETH only")
        now = _integer(now_ms)
        if not isinstance(candles, list) or len(candles) > 5000:
            raise ValueError("Invalid public candle batch")
        parsed = {}
        for row in candles:
            if not isinstance(row, dict):
                raise ValueError("Invalid public candle")
            start, end = _integer(row.get("t")), _integer(row.get("T"))
            if start % BAR_MS or end not in (start + BAR_MS - 1, start + BAR_MS) or end >= now:
                raise ValueError("Need verified closed 15m candles")
            if row.get("s", coin) != coin or row.get("i", "15m") != "15m":
                raise ValueError("Public candle market/interval mismatch")
            o, h, l, c, v = (_number(row.get(field)) for field in ("o", "h", "l", "c", "v"))
            if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
                raise ValueError("Invalid public OHLCV range")
            values = (start + BAR_MS - 1, o, h, l, c, v)
            if start in parsed and parsed[start] != values:
                raise ValueError("Conflicting public candle duplicate")
            parsed[start] = values
        times = sorted(parsed)
        if any(b - a != BAR_MS for a, b in zip(times, times[1:])):
            raise ValueError("Public candle batch has gaps")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT MIN(t) AS first,MAX(t) AS last FROM learning_candles WHERE coin=?", (coin,)).fetchone()
            if times and stored["first"] is not None and (times[0] > stored["last"] + BAR_MS or times[-1] < stored["first"] - BAR_MS):
                raise ValueError("Public candle history has a gap")
            inserted = 0
            for start in times:
                existing = db.execute("SELECT close_ms,open,high,low,close,volume FROM learning_candles WHERE coin=? AND t=?", (coin, start)).fetchone()
                if existing:
                    if tuple(existing) != parsed[start]:
                        raise ValueError("Immutable public candle conflict")
                    continue
                db.execute("INSERT INTO learning_candles VALUES(?,?,?,?,?,?,?,?,?)", (coin, start, *parsed[start], now))
                inserted += 1
            db.commit()
        return {"coin": coin, "inserted": inserted, "duplicates": len(parsed) - inserted,
                "latest_candle_ms": times[-1] + BAR_MS - 1 if times else None}

    @staticmethod
    def _series(db, coin, now):
        rows = db.execute("SELECT * FROM learning_candles WHERE coin=? AND close_ms<? ORDER BY t DESC LIMIT ?",
                          (coin, now, MAX_SERIES)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _examples(self, db, series):
        for coin, bars in series.items():
            for index in range(59, len(bars) - 5):
                decision = bars[index]["close_ms"]
                if (decision + 1) % HOUR_MS:
                    continue
                if db.execute("SELECT 1 FROM learning_examples WHERE coin=? AND decision_ms=? LIMIT 1", (coin, decision)).fetchone():
                    continue
                window = bars[index-59:index+6]
                if any(b["t"] - a["t"] != BAR_MS for a, b in zip(window, window[1:])):
                    continue
                try:
                    features = _features(window[:60])
                except ValueError:
                    continue
                entry, exit_bar = bars[index+2], bars[index+5]
                for side in ("LONG", "SHORT"):
                    net, label = _outcome(entry["open"], exit_bar["close"], side)
                    db.execute("INSERT INTO learning_examples(coin,decision_ms,direction,features,entry_ms,exit_ms,entry_price,exit_price,net_return,label) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)", (coin, decision, side, _json(_direction(features, side)),
                                entry["t"], exit_bar["close_ms"], entry["open"], exit_bar["close"], net, label))

    @staticmethod
    def _mature_predictions(db, now):
        rows = db.execute("SELECT * FROM learning_predictions WHERE status='PENDING' AND deadline_ms<? ORDER BY deadline_ms LIMIT 2000", (now,)).fetchall()
        for prediction in rows:
            bars = db.execute("SELECT * FROM learning_candles WHERE coin=? AND t>=? AND close_ms<=? ORDER BY t",
                              (prediction["coin"], prediction["entry_ms"], prediction["deadline_ms"])).fetchall()
            if len(bars) != 4 or bars[0]["t"] != prediction["entry_ms"] or bars[-1]["close_ms"] != prediction["deadline_ms"]:
                continue
            if any(b["t"] - a["t"] != BAR_MS for a, b in zip(bars, bars[1:])):
                continue
            if prediction["created_ms"] >= prediction["entry_ms"]:
                raise ValueError("Invalid retrospective forward prediction")
            net, label = _outcome(bars[0]["open"], bars[-1]["close"], prediction["direction"])
            db.execute("UPDATE learning_predictions SET status='MATURED',label=?,net_return=?,settled_ms=? "
                       "WHERE coin=? AND decision_ms=? AND direction=? AND status='PENDING'",
                       (label, net, now, prediction["coin"], prediction["decision_ms"], prediction["direction"]))

    def _update_model(self, db, now):
        previous = db.execute("SELECT * FROM learning_models ORDER BY version DESC LIMIT 1").fetchone()
        bootstrap = self._meta(db, "bootstrap")
        if bootstrap is None:
            groups = [row[0] for row in db.execute("SELECT DISTINCT decision_ms FROM learning_examples ORDER BY decision_ms")]
            if len(groups) < MIN_TRAIN_GROUPS + MIN_TEST_GROUPS + 2:
                return
            split = min(len(groups) - MIN_TEST_GROUPS, int(len(groups) * .8))
            test_start = groups[split]
            cutoff = test_start - 6 * BAR_MS
            if sum(stamp <= cutoff for stamp in groups) < MIN_TRAIN_GROUPS:
                return
            bootstrap = {"train_cutoff_ms": cutoff, "test_start_ms": test_start,
                         "last_decision_ms": groups[-1], "purge_ms": 6 * BAR_MS}
            self._set_meta(db, "bootstrap", bootstrap)
        # Initial test and purge intervals are never reused for fitting.
        db.execute("UPDATE learning_examples SET partition=CASE WHEN decision_ms<=? THEN 'TRAIN' "
                   "WHEN decision_ms<? THEN 'PURGE' WHEN decision_ms<=? THEN 'TEST' ELSE 'TRAIN' END WHERE partition IS NULL",
                   (bootstrap["train_cutoff_ms"], bootstrap["test_start_ms"], bootstrap["last_decision_ms"]))
        # Late historical backfill cannot masquerade as a new online observation.
        if previous:
            db.execute("UPDATE learning_examples SET partition='LATE_HISTORY' WHERE partition='TRAIN' AND trained_version IS NULL AND decision_ms<=?",
                       (previous["last_decision_ms"],))
        fresh = db.execute("SELECT * FROM learning_examples WHERE partition='TRAIN' AND trained_version IS NULL ORDER BY decision_ms,coin,direction LIMIT ?",
                           (MAX_TRAIN_ROWS,)).fetchall()
        if not fresh:
            return
        scaler = _loads(previous["scaler"]) if previous else _scaler(fresh)
        weights = _fit(fresh, _loads(previous["weights"]) if previous else [0.] * 9, scaler)
        samples = (previous["train_samples"] if previous else 0) + len(fresh)
        positives = (previous["train_positives"] if previous else 0) + sum(row["label"] for row in fresh)
        baseline = (positives + 1) / (samples + 2)
        evidence = {"new_samples": len(fresh), "first_new_decision_ms": fresh[0]["decision_ms"],
                    "last_new_decision_ms": fresh[-1]["decision_ms"], "epochs": 5, "l2": .001,
                    "bootstrap": bootstrap, "validation_applies_to_bootstrap_only": True,
                    "online_metrics": "frozen prequential probabilities evaluated before fitting labels"}
        cursor = db.execute("INSERT INTO learning_models(created_ms,trained_through_ms,last_decision_ms,weights,scaler,train_samples,train_positives,baseline,evidence) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (now, max(row["exit_ms"] for row in fresh), fresh[-1]["decision_ms"],
                            _json(weights), _json(scaler), samples, positives, baseline, _json(evidence)))
        version = cursor.lastrowid
        db.executemany("UPDATE learning_examples SET trained_version=? WHERE coin=? AND decision_ms=? AND direction=?",
                       [(version, row["coin"], row["decision_ms"], row["direction"]) for row in fresh])
        if previous is None:
            tests = db.execute("SELECT * FROM learning_examples WHERE partition='TEST' ORDER BY decision_ms,coin,direction").fetchall()
            values = [{"decision_ms": row["decision_ms"], "label": row["label"], "baseline": baseline,
                       "probability": _probability(_loads(row["features"]), weights, scaler)} for row in tests]
            validation = _metrics(values, "RETROSPECTIVE_BOOTSTRAP", model_version=version)
            validation.update(train_groups=len({row["decision_ms"] for row in fresh}),
                              test_start_ms=bootstrap["test_start_ms"], train_cutoff_ms=bootstrap["train_cutoff_ms"],
                              purge_ms=bootstrap["purge_ms"], frozen=True)
            self._set_meta(db, "bootstrap_validation", validation)

    @staticmethod
    def _predict(db, series, now):
        model = db.execute("SELECT * FROM learning_models ORDER BY version DESC LIMIT 1").fetchone()
        if model is None:
            return
        weights, scaler = _loads(model["weights"]), _loads(model["scaler"])
        for coin, bars in series.items():
            if len(bars) < 60:
                continue
            # Only the most recently completed hourly feature bar is eligible.
            eligible = [i for i in range(59, len(bars)) if (bars[i]["close_ms"] + 1) % HOUR_MS == 0]
            if not eligible:
                continue
            index = eligible[-1]
            decision = bars[index]["close_ms"]
            entry = decision + BAR_MS + 1
            deadline = decision + 5 * BAR_MS
            if not decision < now < entry or now - decision > 120_000 or model["created_ms"] > now:
                continue  # Never backfill a prediction after its proposed entry.
            try:
                features = _features(bars[index-59:index+1])
            except ValueError:
                continue
            for side in ("LONG", "SHORT"):
                vector = _direction(features, side)
                db.execute("INSERT OR IGNORE INTO learning_predictions(coin,decision_ms,direction,created_ms,entry_ms,deadline_ms,model_version,probability,baseline,features) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)", (coin, decision, side, now, entry, deadline, model["version"],
                           _probability(vector, weights, scaler), model["baseline"], _json(vector)))

    def train(self, now_ms):
        now = _integer(now_ms)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous_time = self._meta(db, "last_train_ms")
            if previous_time is not None and now < previous_time:
                raise ValueError("Out-of-order research training time")
            series = {coin: self._series(db, coin, now) for coin in ("BTC", "ETH")}
            self._examples(db, series)
            self._mature_predictions(db, now)  # Score immutable forecasts first.
            self._update_model(db, now)
            self._predict(db, series, now)
            self._set_meta(db, "last_train_ms", now)
            db.commit()
        return self.summary(now)

    def summary(self, now_ms=None):
        if now_ms is not None:
            _integer(now_ms)
        with closing(self._connect()) as db:
            waters = {coin: {"candles": 0, "latest_candle_ms": None} for coin in ("BTC", "ETH")}
            for row in db.execute("SELECT coin,COUNT(*) AS candles,MAX(close_ms) AS latest FROM learning_candles GROUP BY coin"):
                waters[row["coin"]] = {"candles": row["candles"], "latest_candle_ms": row["latest"]}
            groups = {row["partition"]: row["groups"] for row in db.execute(
                "SELECT partition,COUNT(DISTINCT decision_ms) AS groups FROM learning_examples GROUP BY partition")}
            counts = {"candles": sum(value["candles"] for value in waters.values()),
                      "examples": db.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0],
                      "train_groups": groups.get("TRAIN", 0), "test_groups": groups.get("TEST", 0),
                      "purged_groups": groups.get("PURGE", 0),
                      "forward_predictions": db.execute("SELECT COUNT(*) FROM learning_predictions").fetchone()[0],
                      "forward_matured": db.execute("SELECT COUNT(*) FROM learning_predictions WHERE status='MATURED'").fetchone()[0],
                      "model_versions": db.execute("SELECT COUNT(*) FROM learning_models").fetchone()[0]}
            current = db.execute("SELECT * FROM learning_models ORDER BY version DESC LIMIT 1").fetchone()
            model = None
            if current:
                model = {key: current[key] for key in ("version", "created_ms", "trained_through_ms", "train_samples")}
                model.update(features=FEATURES, scaler_basis="bootstrap_train_only_frozen", method="logistic_sgd_l2")
            validation = self._meta(db, "bootstrap_validation")
            mature = [dict(row) for row in db.execute("SELECT decision_ms,label,probability,baseline FROM learning_predictions "
                                                     "WHERE status='MATURED' ORDER BY decision_ms DESC LIMIT 10000")]
            forward = _metrics(mature, "FORWARD_PREQUENTIAL")
            forward.update(across_model_versions=True, sample_window_limit=10000,
                           note="Frozen predictions scored before online training; not a current-model holdout")
            predictions = []
            for row in db.execute("SELECT * FROM learning_predictions ORDER BY decision_ms DESC,coin,direction LIMIT 12"):
                item = {key: row[key] for key in ("coin", "direction", "decision_ms", "created_ms", "entry_ms", "deadline_ms", "model_version", "status")}
                item["probability_positive_net"] = row["probability"]
                item["probability_calibrated"] = False
                predictions.append(item)
        result = {"status": "RESEARCH_MODEL" if model else "WAITING_DATA",
                  "reason": "public_rules_features_logistic_research_only" if model else "insufficient_matured_history",
                  "real_execution_available": False, "counts": counts, "watermarks": waters,
                  "model": model, "validation": validation, "forward": forward, "predictions": predictions,
                  "costs": {"fee_bps_each_side": 5, "slippage_bps_each_side": 5, "funding_included": False},
                  "label_policy": {"decision_grid": "hourly", "features": "past 60 closed 15m candles",
                                   "waiting_bars": 1, "entry": "i+2 OPEN", "exit": "i+5 CLOSE",
                                   "holding_minutes": 60, "purge_minutes": 90,
                                   "target": "positive unleveraged net return under assumed costs"},
                  "minimum_history": {"train_groups": 100, "test_groups": 40},
                  "warning": "Research probabilities are uncalibrated; no live permission or profitability guarantee"}
        _json(result)
        return result
