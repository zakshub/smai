# SMAI — Faceless Instagram Automation Agentic Tool

**SMAI (Social Media AI)** is a Python-first, local-first multi-agent studio for running **faceless Instagram pages** (Reels, Carousels, and Stories) with human-in-the-loop governance and closed-loop analytics.

## Pipeline

1. **Brand Brain (`configs/brands/*.yaml`)**: Persistent niche pillars, tone, banned clichés, color palette, watermark, and CTA bank.
2. **Scout Agent**: Discovers trending topics (RSS, Reddit, web feeds, or niche seeds), scores viral potential, and deduplicates against history.
3. **Writer Agent**: Generates Reel scripts (3s hook, spoken beats, visual cues), 5–7 slide Carousels, Stories, captions, hashtags, and 2 A/B hook variants.
4. **Media Producer**:
   - **Carousels**: Jinja2 HTML/CSS templates rendered via Playwright Chromium (with Pillow fallback) to crisp `1080x1350` PNGs.
   - **Reels**: `edge-tts` neural voiceover + `WordBoundary` captions -> `.ass` burned-in subtitles + Pexels/procedural B-roll -> FFmpeg `1080x1920` MP4 with `-14 LUFS` audio normalization.
5. **Critic / QA Agent**: Automated resolution, duration, slide text-density, banned-word, and hook quality checks before approval.
6. **Approval Studio & Smart Scheduler**: FastAPI + HTMX dashboard with mobile IG preview, A/B hook selector, and randomized posting windows (`Asia/Karachi` by default).
7. **Publishing & Insights Loop**: Official **Meta Instagram Graph API** publisher + local export bundle mode + `+24h` / `+72h` Insights feedback loop weighted toward **Saves & Shares**.

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium

smai init
smai scout --limit 5
smai generate --format carousel
smai generate --format reel
smai serve
```

Open `http://127.0.0.1:8000` to preview, edit, approve, export, or schedule posts.
