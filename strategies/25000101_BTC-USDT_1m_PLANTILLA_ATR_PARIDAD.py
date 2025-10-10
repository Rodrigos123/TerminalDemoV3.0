# strategies/ma_cross_1m.py
MAGIC = 25000101
SYMBOL = "BTC-USDT"
TIMEFRAME = "1m"

# Parámetros
FAST = 9
SLOW = 21
ATR_LEN = 14
ATR_K_SL = 2.0
ATR_K_TP = 3.0
RISK_USDT = 10.0   # arriesgar ~10 USDT por trade
MIN_PRICE = 1e-8

# Helpers
def _val(x, k):
    # Soporta dict o objeto con atributo
    return x[k] if isinstance(x, dict) else getattr(x, k)

def _series(candles, key):
    return [_val(c, key) for c in candles]

def _sma(arr, n):
    if len(arr) < n: return [None]*len(arr)
    out = [None]*(n-1)
    s = sum(arr[:n])
    out.append(s/n)
    for i in range(n, len(arr)):
        s += arr[i] - arr[i-n]
        out.append(s/n)
    return out

def _atr(candles, n):
    if len(candles) < n+1: return [None]*len(candles)
    highs = _series(candles, "high")
    lows = _series(candles, "low")
    closes = _series(candles, "close")
    trs = []
    for i in range(len(candles)):
        if i == 0:
            trs.append(highs[i]-lows[i])
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)
    # EMA ATR tradicional; para simplicidad uso SMA aquí
    return _sma(trs, n)

def _estimate_lots(risk_usdt, entry, sl):
    # Tamaño aproximado (base_ccy) para riesgo en USDT ~ (entry - sl)*qty ≈ risk_usdt
    dist = abs(entry - sl)
    if dist <= MIN_PRICE: return 0.0
    qty = risk_usdt / dist
    return round(qty, 6)

def run(candles, has_position: bool):
    if not candles or len(candles) < max(SLOW, ATR_LEN)+1:
        return {"action": "NONE"}

    closes = _series(candles, "close")
    highs  = _series(candles, "high")
    lows   = _series(candles, "low")

    sma_f = _sma(closes, FAST)
    sma_s = _sma(closes, SLOW)
    atr   = _atr(candles, ATR_LEN)

    # Usamos la vela cerrada más reciente (índice -1)
    c = closes[-1]
    af = sma_f[-1]; aslow = sma_s[-1]; a = atr[-1]

    if af is None or aslow is None or a is None:
        return {"action": "NONE"}

    # Señales simples: cruce alcista / bajista (mirando las dos últimas velas cerradas)
    af_prev, aslow_prev = sma_f[-2], sma_s[-2]
    action = "NONE"
    side = None
    reason = ""

    if not has_position:
        # OPEN si cruza hacia arriba (golden cross)
        if af_prev is not None and aslow_prev is not None and af_prev <= aslow_prev and af > aslow:
            side = "buy"
            sl = c - ATR_K_SL * a
            tp = c + ATR_K_TP * a
            est_lots = _estimate_lots(RISK_USDT, c, sl)
            action = "OPEN"
            reason = "MA cross up"
            return {"action": action, "side": side, "est_lots": est_lots, "tp": tp, "sl": sl, "reason": reason}

        # OPEN si cruza hacia abajo (death cross)
        if af_prev is not None and aslow_prev is not None and af_prev >= aslow_prev and af < aslow:
            side = "sell"
            sl = c + ATR_K_SL * a
            tp = c - ATR_K_TP * a
            est_lots = _estimate_lots(RISK_USDT, c, sl)
            action = "OPEN"
            reason = "MA cross down"
            return {"action": action, "side": side, "est_lots": est_lots, "tp": tp, "sl": sl, "reason": reason}

        return {"action": "NONE"}

    else:
        # Con posición: cierre por cruces inversos (exit al cierre de vela)
        # No sabemos el lado actual aquí; el terminal guarda eso en su estado.
        # Estrategia: si fast cruza en sentido opuesto al de la entrada, pedimos CLOSE.
        # (El motor decidirá el 'close_side' correcto automáticamente)
        if af_prev is not None and aslow_prev is not None:
            crossed_up = af_prev <= aslow_prev and af > aslow
            crossed_dn = af_prev >= aslow_prev and af < aslow
            if crossed_up or crossed_dn:
                return {"action": "CLOSE", "reason": "Opposite MA cross"}
        return {"action": "NONE"}


def run(candles, has_position: bool):
    """Regla solicitada:
    - Abrir cuando el último dígito ENTERO del precio de cierre (int(close) % 2) sea IMPAR.
    - Cerrar cuando ese dígito sea PAR.
    - SL/TP por ATR si está disponible; fallback 0.5%/1% si no.
    Evaluación usando la última vela CERRADA del TF 1m.
    """
    if not candles or len(candles) < 2:
        return {"action":"NONE"}

    # Obtener cierre de la última vela
    try:
        c = float(candles[-1]["close"])
    except Exception:
        return {"action":"NONE"}
    if c <= 0:
        return {"action":"NONE"}

    # ATR si la función existe
    atr = None
    try:
        atr = _atr(candles, ATR_LEN)[-1]
    except Exception:
        atr = None

    if atr is None or not isinstance(atr, (float, int)) or atr <= 0:
        sl_long = c * (1 - 0.005); tp_long = c * (1 + 0.010)
        sl_short = c * (1 + 0.005); tp_short = c * (1 - 0.010)
    else:
        sl_long = c - ATR_K_SL * atr; tp_long = c + ATR_K_TP * atr
        sl_short = c + ATR_K_SL * atr; tp_short = c - ATR_K_TP * atr

    last_digit = int(c) % 2

    if not has_position:
        if last_digit == 1:  # impar => abrir (lado BUY para pruebas)
            sl = sl_long; tp = tp_long; side = "buy"
            try:
                est_lots = _estimate_lots(RISK_USDT, c, sl)
            except Exception:
                est_lots = 0.0
            if est_lots and est_lots > 0:
                return {"action":"OPEN","side":side,"est_lots":est_lots,"tp":tp,"sl":sl,"reason":"ULTIMO DIGITO IMPAR/PAR"}
        return {"action":"NONE"}
    else:
        if last_digit == 0:  # par => cerrar
            return {"action":"CLOSE","reason":"ULTIMO DIGITO IMPAR/PAR"}
        return {"action":"NONE"}
