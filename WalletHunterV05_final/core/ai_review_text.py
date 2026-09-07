"""Plain text shared by Telegram; no unescaped HTML from markets or users."""


def review_text(row, english=False):
    p = row["payload"]
    f = p["factors"]
    t = lambda ru, en: en if english else ru
    action = t("Сократить", "Reduce") if p["action"] == "REDUCE" else t("Наблюдать, без сделки", "Observe, no trade")
    lines = [t("🧠 AI · ПРОВЕРКА ПОЗИЦИИ", "🧠 AI · POSITION REVIEW"),
             f'{p["position"]["coin"]} · {p["position"]["side"]} · ROE {p["roe"]:.2f}%',
             f'{action}: {p["size"]:g} · ≈ ${p["notional_usdc"]:.2f}',
             t("Новых средств: $0; плечо не меняется.", "New funds: $0; leverage unchanged."),
             t("Остаток позиции", "Remaining size") + f': {p["remaining_size"]:g}',
             f'EMA20/50: {f["trend_ema20_50"]["ema20"]:.4g} / {f["trend_ema20_50"]["ema50"]:.4g}',
             f'RSI14: {f["rsi14"]:.1f} · MACD: {f["macd_hist"]:.4g}',
             f'ATR14: {f["atr14_pct"]:.2f}% · ' + t("Объём/средний", "Volume/average") + f': {f["volume_ratio20"]}',
             t("Поддержка / сопротивление", "Support / resistance") + f': {f["levels20"]["support"]:.5g} / {f["levels20"]["resistance"]:.5g}',
             f'Funding: {f["funding_bps_hour"]:.3f} bps/h · OI: {f["open_interest"]:g}',
             t("Основание: EMA и MACD против позиции.", "Reason: EMA and MACD oppose the position.") if p["action"] == "REDUCE" else
             t("Данных для обоснованного исполнимого действия недостаточно.", "No supported executable action."),
             t("Это правила по данным Hyperliquid, не обученная модель. OI — текущий снимок, не тренд.",
               "Hyperliquid data with explicit rules, not a trained model. OI is a snapshot, not a trend."),
             t("Сокращение фиксирует часть убытка. Есть комиссии; выход в плюс не гарантирован.",
               "Reduction realises part of the loss. Fees apply; recovery is not guaranteed."),
             t("После подтверждения копирование этого инструмента на паузе до отдельного возобновления.",
               "After confirmation, copying this market is held until separately resumed."),
             t("Усреднение/маржа/плечо: недоступны без проверенного дополнительного бюджета.",
               "Averaging/margin/leverage: unavailable without a validated extra budget."),
             t("Предложение действует 5 минут. Нет = без изменений, анализ продолжается.",
               "Valid for 5 minutes. No = no changes; analysis continues.")]
    if not p.get("gate", {}).get("allowed"):
        lines.append(t("Вероятность успеха не рассчитана. Порог 60% не подтверждён; вмешательство заблокировано.",
                       "Success probability unavailable. The 60% threshold is unverified; intervention blocked."))
    return "\n".join(lines)
