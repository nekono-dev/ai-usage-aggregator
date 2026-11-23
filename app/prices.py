"""モデル別単価表と概算コスト計算。

単価は USD / 1M トークン。トークン種別ごとに素直に計算する
（ブレンド単価のような前提は置かない）。

- cache_write の単価が公表されていないモデルは input と同額として計算する。
- モデル名に日付サフィックス等が付く場合（例: claude-sonnet-5-5-20260101）に
  備え、最長一致の前方一致でも引けるようにする。
- 単価表に無いモデルは、ファミリー別の代替単価で計算する。未知のモデルでも
  コスト 0 のまま集計から漏らさないための仕組みで、どの単価で計算したかは
  戻り値の price_source（"table" = 単価表、"fallback" = 代替単価）で区別する。
  代替単価は単価表の中位ティアを参照するので、単価表を更新すれば追随する。
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

# 単価表に無いモデルに使う代替単価（ファミリー接頭辞 -> 単価表のモデル名）。
# Claude 系は Sonnet、GPT / Codex 系は Sol の単価で計算する。
FALLBACK_MODEL: dict[str, str] = {
    "claude": "claude-sonnet-5-5",
    "gpt": "gpt-6.1-sol",
    "codex": "gpt-6.1-sol",
}
DEFAULT_FALLBACK_MODEL = "claude-sonnet-5-5"


def lookup(model: str) -> tuple[float, float, float, float] | None:
    """モデル名から単価表を引く。完全一致 → 最長前方一致の順。無ければ None。"""
    if model in PRICES:
        return PRICES[model]
    best: str | None = None
    for key in PRICES:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return PRICES[best] if best is not None else None


def lookup_with_source(model: str) -> tuple[tuple[float, float, float, float], str]:
    """単価とその出所を返す。単価表に無ければ代替単価（"fallback"）を使う。"""
    rates = lookup(model)
    if rates is not None:
        return rates, "table"
    for prefix, fallback in FALLBACK_MODEL.items():
        if model.startswith(prefix):
            return PRICES[fallback], "fallback"
    return PRICES[DEFAULT_FALLBACK_MODEL], "fallback"


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int, cache_write_tokens: int) -> tuple[float, str]:
    """概算コスト USD と単価の出所（"table" / "fallback"）を返す。

    単価表に無いモデルも代替単価で計算するため、コストが理由なく 0 になる
    モデルは存在しない（トークン数が 0 の場合は当然 0 になる）。
    """
    rates, source = lookup_with_source(model)
    p_in, p_out, p_cr, p_cw = rates
    cost = (input_tokens * p_in + output_tokens * p_out
            + cache_read_tokens * p_cr + cache_write_tokens * p_cw) / 1_000_000
    return cost, source
