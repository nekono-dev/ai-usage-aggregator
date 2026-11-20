"""モデル別単価表と概算コスト計算。

単価は USD / 1M トークン。トークン種別ごとに素直に計算する
（ブレンド単価のような前提は置かない）。

- cache_write の単価が公表されていないモデルは input と同額として計算する。
- 単価表に無いモデルはコスト 0 とし、呼び出し側で "unknown" と分かるようにする。
- モデル名に日付サフィックス等が付く場合（例: claude-sonnet-5-5-20260101）に
  備え、最長一致の前方一致でも引けるようにする。
"""
from __future__ import annotations

# model -> (input, output, cache_read, cache_write) USD per 1M tokens
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "gpt-6.1-sol": (2.00, 10.00, 0.10, 2.50),
    "gpt-6.2-sol": (2.00, 10.00, 0.10, 2.50),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.125),
}


def lookup(model: str) -> tuple[float, float, float, float] | None:
    """モデル名から単価を引く。完全一致 → 最長前方一致の順。無ければ None。"""
    if model in PRICES:
        return PRICES[model]
    best: str | None = None
    for key in PRICES:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return PRICES[best] if best is not None else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int, cache_write_tokens: int) -> tuple[float, bool]:
    """概算コスト USD と、単価が判明したかどうか (False なら unknown) を返す。"""
    rates = lookup(model)
    if rates is None:
        return 0.0, False
    p_in, p_out, p_cr, p_cw = rates
    cost = (input_tokens * p_in + output_tokens * p_out
            + cache_read_tokens * p_cr + cache_write_tokens * p_cw) / 1_000_000
    return cost, True
