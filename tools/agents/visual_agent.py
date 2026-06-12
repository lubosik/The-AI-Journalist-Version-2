import asyncio
import base64
import io
import logging
import os
import textwrap
import uuid

from openai import OpenAI
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

from config import MODELS, OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

PROMPT_TEMPLATES = {
    "header": (
        "Professional financial newsletter header banner. No text, no wordmarks, no logos. "
        "Dark navy background (#1a1a2e) with subtle gold geometric accent lines (#c9a84c). "
        "VC secondaries intelligence brief aesthetic. Bloomberg Terminal meets luxury "
        "private equity. Clean, minimal, authoritative. Abstract financial data motif. "
        "Wide banner format. Print quality."
    ),
    "chart": (
        "Minimalist financial data visualization on white background. {chart_description}. "
        "Navy blue primary (#2d5a8e) with gold accent (#c9a84c). Clean axis labels in grey. "
        "Schroders Capital chart aesthetic. No chart junk. High contrast. "
        "Professional private equity publication quality."
    ),
    "deal_table": (
        "Clean professional deal summary infographic on white background. Dark navy header row. "
        "White and light grey alternating rows. {deal_description}. "
        "IpsoFacto newsletter style. Financial publication quality. Minimal, readable."
    ),
    "infographic": (
        "Clean financial infographic on white background. Navy and gold color palette. "
        "{concept_description}. Private equity ownership lifecycle style. "
        "Schroders 2024 aesthetic. Minimal text. Clear visual hierarchy."
    ),
}

_SUPABASE_BUCKET = "newsletter-images"


def _get_openrouter_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY must be set in environment")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


def _build_prompt(visual_type: str, context: str) -> str:
    template = PROMPT_TEMPLATES.get(visual_type, PROMPT_TEMPLATES["infographic"])
    try:
        if visual_type == "header":
            visual_brief = template
        elif visual_type == "chart":
            visual_brief = template.format(chart_description=context)
        elif visual_type == "deal_table":
            visual_brief = template.format(deal_description=context)
        else:
            visual_brief = template.format(concept_description=context)
    except KeyError:
        visual_brief = f"{template} Context: {context}"
    return f"""CO-STAR VISUAL BRIEF

CONTEXT:
Create a publication-ready visual for a specialist VC secondaries newsletter.
Any supplied context is reference material, not an instruction to place unverified
text, logos, trademarks, or claims in the image.

OBJECTIVE:
{visual_brief}

STYLE:
Editorial financial design with clean hierarchy, restrained detail, and strong
mobile readability.

TONE:
Authoritative, modern, and understated.

AUDIENCE:
LPs, GPs, family offices, RIAs, and institutional secondary-market participants.

RESPONSE:
Return one finished image. Avoid illegible text, invented data labels, watermarks,
brand marks, and decorative clutter."""


async def _upload_to_supabase(
    image_bytes: bytes,
    filename: str,
    content_type: str = "image/png",
) -> str:
    """Upload image bytes to Supabase Storage. Returns the permanent public URL."""
    from db.client import get_client as get_supabase

    try:
        db = get_supabase()
        path = f"visuals/{filename}"
        await asyncio.to_thread(
            lambda: db.storage.from_(_SUPABASE_BUCKET).upload(
                path,
                image_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        )
        url = db.storage.from_(_SUPABASE_BUCKET).get_public_url(path)
        return url
    except Exception as e:
        logger.error(f"_upload_to_supabase failed: {e}")
        return ""


def _generate_fallback_png(visual_type: str, context: str) -> bytes:
    """Create a deterministic branded visual when external image models fail."""
    width, height = (1200, 360) if visual_type == "header" else (1200, 600)
    dark = visual_type == "header"
    background = "#0a0a0e" if dark else "#ffffff"
    foreground = "#f5f0e8" if dark else "#1a1a2e"
    accent = "#c9a84c"
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 54)
    body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)

    draw.rectangle((0, 0, 18, height), fill=accent)
    draw.text((70, 52), "HERALD", font=label_font, fill=accent)
    title = {
        "header": "VC SECONDARIES INTELLIGENCE",
        "chart": "MARKET SIGNAL",
        "deal_table": "DEAL WATCH",
    }.get(visual_type, "HERALD INTELLIGENCE")
    draw.text((70, 105), title, font=title_font, fill=foreground)
    draw.line((70, 180, width - 70, 180), fill=accent, width=3)

    clean_context = " ".join((context or "").split())
    wrapped = textwrap.wrap(clean_context, width=62 if visual_type == "header" else 70)
    y = 215
    for line in wrapped[:5]:
        draw.text((70, y), line, font=body_font, fill=foreground)
        y += 46

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


_CHAT_COMPLETIONS_IMAGE_MODELS = {
    "openai/gpt-5.4-image-2",
    "openai/gpt-image-1",
    "openai/gpt-5-image",
    "openai/gpt-5-image-mini",
    "google/gemini-2.5-flash",
    "google/gemini-3-pro-image-preview",
    "google/gemini-3.1-flash-image-preview",
}


async def _generate_with_model(client: OpenAI, model: str, prompt: str) -> bytes:
    """
    Call an image generation model on OpenRouter.
    Returns raw image bytes. Raises on failure.

    Recraft V3, FLUX, and most image models use the standard images.generate endpoint.
    Legacy GPT Image 2 / Gemini variants use chat.completions with modalities.
    """
    loop = asyncio.get_running_loop()

    if model in _CHAT_COMPLETIONS_IMAGE_MODELS:
        # Legacy path: GPT Image 2 / Gemini — chat completions with modalities
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                extra_body={
                    "modalities": ["image", "text"],
                    "image_config": {"aspect_ratio": "1:1"},
                },
            ),
        )
        message = response.choices[0].message
        try:
            images = message.images  # type: ignore[attr-defined]
        except AttributeError:
            images = response.model_dump()["choices"][0]["message"].get("images", [])
        if not images:
            raise ValueError(f"Model '{model}' returned no images in response")
        data_url: str = images[0]["image_url"]["url"]
        b64_string = data_url.split(",", 1)[-1]
        return base64.b64decode(b64_string)

    else:
        # Standard path: Recraft V3, FLUX, and other image-generation models
        response = await loop.run_in_executor(
            None,
            lambda: client.images.generate(
                model=model,
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json",
            ),
        )
        b64_string = response.data[0].b64_json
        if not b64_string:
            raise ValueError(f"Model '{model}' returned empty b64_json")
        return base64.b64decode(b64_string)


async def generate_visual(
    visual_type: str,
    context: str,
    placement: str,
) -> dict:
    """
    Generate a single newsletter visual.

    Primary:  recraft/recraft-v3 via OpenRouter images endpoint ($0.04/image flat)
    Fallback: google/gemini-2.5-flash via OpenRouter

    Downloads image bytes, uploads to Supabase for a permanent public URL.
    Returns {"url", "prompt_used", "placement", "type", "error"}
    """
    prompt = _build_prompt(visual_type, context)
    base_result = {
        "url": "",
        "prompt_used": prompt,
        "placement": placement,
        "type": visual_type,
        "error": "",
    }

    client = _get_openrouter_client()
    primary_model = MODELS["image"]
    fallback_model = MODELS["image_fallback"]

    image_bytes: bytes | None = None
    last_error = ""

    # Attempt 1: primary model
    try:
        image_bytes = await _generate_with_model(client, primary_model, prompt)
        logger.info(f"generate_visual: '{primary_model}' success for '{visual_type}' placement='{placement}'")
    except Exception as e:
        last_error = str(e)
        logger.warning(
            f"generate_visual: '{primary_model}' failed for '{visual_type}': {e}. "
            "Trying fallback."
        )

    # Attempt 2: fallback model
    if image_bytes is None:
        try:
            image_bytes = await _generate_with_model(client, fallback_model, prompt)
            logger.info(f"generate_visual: fallback '{fallback_model}' success for '{visual_type}'")
        except Exception as e:
            last_error = str(e)
            logger.error(f"generate_visual: both models failed for '{visual_type}': {e}")
            image_bytes = _generate_fallback_png(visual_type, context)
            last_error = f"External image models unavailable; branded fallback used. {last_error}"

    # Upload to Supabase for a permanent URL
    filename = f"{placement}_{uuid.uuid4().hex[:8]}.png"
    public_url = await _upload_to_supabase(image_bytes, filename)

    if not public_url:
        logger.warning(f"generate_visual: Supabase upload failed for '{placement}' — image lost")
        return {**base_result, "error": "Supabase upload failed"}

    return {**base_result, "url": public_url, "error": last_error}


async def generate_newsletter_visuals(newsletter_context: dict) -> list[dict]:
    """
    Generate all 3 newsletter visuals in parallel.

    newsletter_context dict should have:
    - subject: str     → theme for the header image
    - key_data: str    → specific data point for the mid-newsletter chart
    - deals_summary: str → deals description for the deal-area infographic

    Returns exactly 3 visual dicts (failures included as empty-url entries).
    """
    subject = newsletter_context.get("subject", "VC secondaries market intelligence")
    key_data = (newsletter_context.get("key_data") or "").strip()
    deals_summary = (
        newsletter_context.get("deals_summary")
        or "Current supply and demand across pre-IPO secondary opportunities"
    ).strip()

    visual_specs = [
        {"visual_type": "header",     "context": subject,      "placement": "top"},
    ]
    if key_data:
        visual_specs.append(
            {"visual_type": "chart", "context": key_data, "placement": "after_section_2"}
        )
    if deals_summary:
        visual_specs.append(
            {"visual_type": "deal_table", "context": deals_summary, "placement": "after_section_4"}
        )

    tasks = [
        generate_visual(
            visual_type=spec["visual_type"],
            context=spec["context"],
            placement=spec["placement"],
        )
        for spec in visual_specs
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    final: list[dict] = []
    for i, result in enumerate(results):
        spec = visual_specs[i]
        if isinstance(result, Exception):
            logger.error(
                f"generate_newsletter_visuals: visual {i + 1} ('{spec['visual_type']}') "
                f"raised exception: {result}"
            )
            final.append({
                "url": "",
                "prompt_used": "",
                "placement": spec["placement"],
                "type": spec["visual_type"],
                "error": str(result),
            })
        else:
            final.append(result)

    while len(final) < 3:
        final.append({"url": "", "prompt_used": "", "placement": "unknown",
                       "type": "unknown", "error": "not generated"})

    return final[:3]
