"""LLM catalog + preference-weighted ranking for FR Copilot's model routing (shared by the selector Lambda and scripts).

Score per model = w_quality * Q + w_cost * C + w_latency * L, where each criterion is normalized to 0-100 across the
eight candidates (100 = best in the catalog):
  Q = quality: 60% measured FR Copilot task score (scripts/llm_benchmark.py rubric on the C-007 question)
      + 40% general-capability prior set by the instructor (illustrative, from public leaderboards)
  C = cost: measured USD per FR Copilot question (avg tokens x Bedrock list price), log-scaled (cheapest = 100)
  L = latency: measured median end-to-end seconds for the same agent task (fastest = 100)
Only models whose task score >= the eligibility threshold may serve live traffic.
"""
import math

# Static catalog. Prices: USD per 1M tokens, us-east-1 on-demand standard tier (AWS Price List API for third-party
# open-weight models; Anthropic list prices on Bedrock for Claude). Benchmark fields are filled in by llm_benchmark.py.
CATALOG = [
    {"id": "us.anthropic.claude-opus-4-6-v1", "name": "Claude Opus 4.6", "vendor": "Anthropic",
     "strengths": "Highest reasoning quality, careful multi-step tool use; premium price, slower",
     "price_in": 5.00, "price_out": 25.00, "general_quality": 97, "max_tokens": 8192,
     "request_fields": {"thinking": {"type": "adaptive"}}},
    {"id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "vendor": "Anthropic",
     "strengths": "Near-frontier quality and reliable agentic tool use at mid-tier price",
     "price_in": 3.00, "price_out": 15.00, "general_quality": 93, "max_tokens": 8192,
     "request_fields": {"thinking": {"type": "adaptive"}}},
    {"id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "name": "Claude Haiku 4.5", "vendor": "Anthropic",
     "strengths": "Fast, low-cost Claude with strong tool use; good quality per dollar",
     "price_in": 1.00, "price_out": 5.00, "general_quality": 84, "max_tokens": 8192,
     "request_fields": {"thinking": {"type": "enabled", "budget_tokens": 2048}}},
    {"id": "deepseek.v3.2", "name": "DeepSeek V3.2", "vendor": "DeepSeek",
     "strengths": "Open-weight MoE with strong reasoning and coding at low cost",
     "price_in": 0.62, "price_out": 1.85, "general_quality": 86, "max_tokens": 8192, "request_fields": {}},
    {"id": "mistral.mistral-large-3-675b-instruct", "name": "Mistral Large 3", "vendor": "Mistral AI",
     "strengths": "European open-weight flagship; fast, multilingual, low output price",
     "price_in": 0.50, "price_out": 1.50, "general_quality": 81, "max_tokens": 8192, "request_fields": {}},
    {"id": "openai.gpt-oss-120b-1:0", "name": "gpt-oss-120b", "vendor": "OpenAI",
     "strengths": "OpenAI open-weight reasoning model; very low cost",
     "price_in": 0.15, "price_out": 0.60, "general_quality": 80, "max_tokens": 8192, "request_fields": {}},
    {"id": "us.amazon.nova-2-lite-v1:0", "name": "Amazon Nova 2 Lite", "vendor": "Amazon",
     "strengths": "Amazon's fast, low-cost multimodal model with built-in reasoning",
     "price_in": 0.33, "price_out": 2.75, "general_quality": 76, "max_tokens": 8192, "request_fields": {}},
    {"id": "qwen.qwen3-next-80b-a3b", "name": "Qwen3 Next 80B A3B", "vendor": "Qwen (Alibaba)",
     "strengths": "Sparse MoE (3B active): extremely fast and cheap; lighter reasoning",
     "price_in": 0.14, "price_out": 1.20, "general_quality": 74, "max_tokens": 8192, "request_fields": {}},
]

DEFAULT_WEIGHTS = {"quality": 50, "cost": 25, "latency": 25}
ELIGIBILITY_MIN_TASK_SCORE = 80  # benchmark: models below this skipped the calc tools or got key facts wrong
SWITCH_MARGIN = 0.5  # don't switch for a sub-half-point difference (avoids flapping on ties)


def validate_weights(w: dict) -> dict:
    try:
        out = {k: int(w[k]) for k in ("quality", "cost", "latency")}
    except (KeyError, TypeError, ValueError):
        raise ValueError("weights must include integer quality, cost and latency")
    if any(v < 0 or v > 100 for v in out.values()) or sum(out.values()) != 100:
        raise ValueError("each weight must be 0-100 and the three must add up to 100")
    return out


def _minmax(vals, higher_better=True):
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [100.0] * len(vals)
    return [100.0 * ((v - lo) / (hi - lo) if higher_better else (hi - v) / (hi - lo)) for v in vals]


def rank(candidates: list, weights: dict, active_id: str = "", min_task: int = ELIGIBILITY_MIN_TASK_SCORE) -> list:
    """Return candidates sorted by weighted score with the normalized sub-scores attached."""
    benched = [c for c in candidates if c.get("task_score") is not None]
    if not benched:
        return []
    quality = [0.6 * c["task_score"] + 0.4 * c["general_quality"] for c in benched]
    q = _minmax(quality)
    cst = _minmax([math.log10(max(c["cost_per_question_usd"], 1e-6)) for c in benched], higher_better=False)
    lat = _minmax([c["latency_s"] for c in benched], higher_better=False)
    total = sum(weights.values()) or 1
    rows = []
    for i, c in enumerate(benched):
        score = (weights["quality"] * q[i] + weights["cost"] * cst[i] + weights["latency"] * lat[i]) / total
        rows.append({**c, "quality_raw": round(quality[i], 1), "quality_score": round(q[i], 1), "cost_score": round(cst[i], 1),
                     "latency_score": round(lat[i], 1), "score": round(score, 2),
                     "eligible": c["task_score"] >= min_task, "active": c["id"] == active_id})
    rows.sort(key=lambda r: (-r["score"], r["cost_per_question_usd"]))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows


def decide(ranking: list, active_id: str, margin: float = SWITCH_MARGIN) -> dict:
    """Pick the best eligible model and decide whether switching the live app is warranted."""
    eligible = [r for r in ranking if r["eligible"]]
    if not eligible:
        return {"switch": False, "target": active_id, "reason": "No model passed the FR Copilot task-eligibility threshold."}
    best = eligible[0]
    top = ranking[0]
    note = "" if top["eligible"] else (f" {top['name']} ranked #1 but scored {top['task_score']}/100 on the FR Copilot task "
                                       f"(below the eligibility threshold), so it is not used for live traffic.")
    current = next((r for r in ranking if r["id"] == active_id), None)
    if best["id"] == active_id:
        return {"switch": False, "target": active_id, "reason": f"{best['name']} is already the best eligible model for these preferences.{note}"}
    if current and current["eligible"] and best["score"] - current["score"] < margin:
        return {"switch": False, "target": active_id,
                "reason": f"{best['name']} leads {current['name']} by only {best['score'] - current['score']:.2f} points (< {margin}); keeping the current model.{note}"}
    return {"switch": True, "target": best["id"],
            "reason": f"{best['name']} is the best eligible model for these preferences (score {best['score']:.1f}"
                      + (f" vs {current['score']:.1f} for {current['name']}" if current else "") + f").{note}"}
