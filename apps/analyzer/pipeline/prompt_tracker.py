"""
Prompt ranking engine — fires prompts across AI engines multiple times,
extracts signals, computes weighted scores, and ranks prompts.

Inspired by Peec.ai's prompt ranking mechanism:
  Score = (Visibility × 0.5) + (Position Score × 0.3) + (Sentiment × 0.2)
"""
import json
import logging

logger = logging.getLogger("apps")

# Maps internal provider keys → PromptResult.Engine choices
_ENGINE_MAP = {
    "gpt": "chatgpt",
    "claude": "claude",
    "gemini": "gemini",
}

DEFAULT_RUNS = 3  # Fire each prompt N times to handle AI randomness


def generate_brand_prompts(
    brand_name: str,
    brand_url: str,
    industry: str = "",
    page_content: str = "",
    count: int = 10,
) -> list[str]:
    """
    Use AI to generate brand-relevant prompts that real users would ask.

    The prompts MUST NOT mention the brand name — they should be natural
    questions a user would ask when looking for this type of product/service.
    The AI will understand the brand from context and generate relevant prompts.
    """
    from .llm import ask_llm as ask_single_llm

    context_parts = [f"Brand: {brand_name}", f"URL: {brand_url}"]
    if industry:
        context_parts.append(f"Industry: {industry}")
    if page_content:
        context_parts.append(f"Page content (first 1500 chars): {page_content[:1500]}")

    prompt = f"""You are an AI visibility expert. Generate exactly {count} search prompts that real users would type into ChatGPT, Gemini, or Perplexity when looking for products/services like what this brand offers.

{chr(10).join(context_parts)}

RULES:
1. Do NOT mention the brand name in any prompt
2. Write natural conversational questions (how real users ask AI assistants)
3. Cover different intents: comparison, recommendation, how-to, best-of, alternatives
4. Include specific details relevant to this brand's niche
5. Mix broad and specific prompts
6. Use current year context where relevant

Return ONLY a JSON array of {count} prompt strings. No explanations.

Example format: ["What are the best ...", "Compare the top ...", "Which ... do experts recommend?"]"""

    try:
        raw = ask_single_llm(prompt, purpose="Generate Brand Prompts", max_tokens=1000)
        # Parse JSON array from response
        raw = raw.strip()
        # Handle markdown code blocks
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        prompts = json.loads(raw)
        if isinstance(prompts, list) and len(prompts) > 0:
            return [str(p).strip() for p in prompts[:count] if str(p).strip()]
    except Exception as exc:
        logger.warning("generate_brand_prompts failed: %s", exc)

    # Fallback: generic prompts
    return [
        f"What are the best {industry or 'services'} available today?",
        f"Compare the top {industry or 'tools'} for businesses",
        f"Which {industry or 'solution'} do experts recommend?",
        f"What should I look for when choosing a {industry or 'provider'}?",
        f"What are the leading {industry or 'platforms'} in 2026?",
    ]


def fire_prompt_across_engines(
    prompt_text: str,
    brand_name: str,
    brand_url: str,
    runs: int = DEFAULT_RUNS,
) -> list[dict]:
    """
    Fire prompt_text to GPT/Claude/Gemini multiple times to handle randomness.

    With runs=3 and 3 engines → 9 total results.
    Returns list of dicts: engine, response_text, brand_mentioned, sentiment, confidence, rank_position
    """
    from .llm import ask_multiple_llms
    from .ai_visibility import _build_brand_aliases, _match_brand, _analyze_mention_quality, _check_ranking_position

    brand_aliases = _build_brand_aliases(brand_name, brand_url)
    all_results = []

    for run_idx in range(runs):
        try:
            responses = ask_multiple_llms(
                prompt_text,
                providers=["gpt", "claude", "gemini"],
                purpose=f"Prompt Track (run {run_idx + 1}/{runs})",
                max_tokens=512,
            )
        except Exception as exc:
            logger.warning("fire_prompt run %d failed: %s", run_idx + 1, exc)
            continue

        for provider_key, response_text in responses.items():
            engine = _ENGINE_MAP.get(provider_key, provider_key)

            if not response_text:
                all_results.append({
                    "engine": engine,
                    "response_text": "",
                    "brand_mentioned": False,
                    "sentiment": "neutral",
                    "confidence": 0.0,
                    "rank_position": 0,
                })
                continue

            found, confidence, _ = _match_brand(brand_aliases, response_text)

            sentiment = "neutral"
            rank_position = 0
            if found:
                quality = _analyze_mention_quality(response_text, brand_aliases)
                sentiment = quality.get("sentiment", "neutral")
                ranking = _check_ranking_position(response_text, brand_aliases)
                rank_position = ranking.get("rank_position", 0)

            all_results.append({
                "engine": engine,
                "response_text": response_text[:3000],
                "brand_mentioned": found,
                "sentiment": sentiment,
                "confidence": round(confidence, 3),
                "rank_position": rank_position,
            })

    return all_results


def compute_prompt_score(results: list[dict]) -> dict:
    """
    Compute weighted prompt ranking score from multiple results.

    Score = (Visibility × 0.5) + (Position Score × 0.3) + (Sentiment × 0.2)

    Returns dict with: score, visibility_pct, avg_position, sentiment, label
    """
    if not results:
        return {
            "score": 0.0,
            "visibility_pct": 0.0,
            "avg_position": 0,
            "sentiment": "neutral",
            "label": "Weak",
            "total_runs": 0,
            "mentions": 0,
        }

    total = len(results)
    mentions = sum(1 for r in results if r.get("brand_mentioned"))

    # Visibility: % of runs where brand appears (0-1)
    visibility = mentions / total if total else 0

    # Position Score: average 1/position (higher = better)
    positions = [r.get("rank_position", 0) for r in results if r.get("rank_position", 0) > 0]
    avg_position_score = (sum(1.0 / p for p in positions) / len(positions)) if positions else 0

    # Sentiment: +1 positive, 0 neutral, -1 negative → normalize to 0-1
    sentiment_map = {"positive": 1, "neutral": 0, "negative": -1}
    sentiments = [sentiment_map.get(r.get("sentiment", "neutral"), 0) for r in results]
    raw_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
    norm_sentiment = (raw_sentiment + 1) / 2  # -1..1 → 0..1

    # Weighted score
    score = (visibility * 0.5) + (avg_position_score * 0.3) + (norm_sentiment * 0.2)
    score = min(score, 1.0)  # Cap at 1.0

    # Determine overall sentiment label
    if raw_sentiment > 0.3:
        sentiment_label = "positive"
    elif raw_sentiment < -0.3:
        sentiment_label = "negative"
    else:
        sentiment_label = "neutral"

    # Ranking label
    if score >= 0.6:
        label = "Strong"
    elif score >= 0.3:
        label = "Moderate"
    else:
        label = "Weak"

    return {
        "score": round(score, 3),
        "visibility_pct": round(visibility * 100, 1),
        "avg_position": round(1.0 / avg_position_score, 1) if avg_position_score > 0 else 0,
        "sentiment": sentiment_label,
        "label": label,
        "total_runs": total,
        "mentions": mentions,
    }


def recheck_track(track, brand_name: str, brand_url: str) -> int:
    """
    Re-fire a PromptTrack across all configured engines (3 runs × 3 engines),
    save PromptResult rows, compute and update the prompt score.

    Returns the number of new PromptResult rows created.
    """
    from django.db import close_old_connections
    from apps.analyzer.models import PromptResult

    close_old_connections()
    engine_results = fire_prompt_across_engines(track.prompt_text, brand_name, brand_url)
    created = 0
    for r in engine_results:
        PromptResult.objects.create(prompt_track=track, **r)
        created += 1

    # Compute and save score from ALL results (not just this run)
    all_results = list(
        track.results.values("brand_mentioned", "sentiment", "rank_position", "confidence")
    )
    score_data = compute_prompt_score(all_results)
    track.score = score_data["score"]
    track.save(update_fields=["score"])

    logger.info(
        "recheck_track #%d ('%s'): %d new results, score=%.3f (%s)",
        track.pk, track.prompt_text[:60], created, score_data["score"], score_data["label"],
    )
    return created
