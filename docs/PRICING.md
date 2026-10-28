# Plans

These are proposals. They're modelled on OpusClip's public tiers as reported by third-party
blogs (Castmagic and Creatify, August 2026). Confirm OpusClip's current pricing before you
publish anything. Source of truth in code: `PLANS` in `webapp/service.py`.

| | Free | Creator | Pro | Business |
|---|---|---|---|---|
| Price | $0 | $15/mo | $29/mo | $79/mo |
| Source minutes / month | 60 | 300 | 600 | 1,500 |
| Max upload | 2 GB | 4 GB | 8 GB | 8 GB |
| Clips per upload | 5 | 15 | 20 | 30 |
| Logo watermark | Yes | No | No | No |
| Clip storage | 7 days | 30 days | 90 days | 90 days |
| Publish to YouTube / TikTok | No | No | Yes, once approved | Yes, once approved |
| Brand kit | No | No | Yes | Yes |
| Seats | 1 | 1 | 1 | 3 |

## What's enforced today

- Source minutes per month (measured with ffprobe at upload).
- Max upload size, clips per upload, and the watermark flag (applied by the worker).

## Not enforced yet

- Clip storage retention (the field is shown in the UI, but nothing deletes files).
- Seats and team sharing.
- Publishing and the brand kit. These depend on the platform approvals in `LAUNCH_BLOCKERS.md`.
- Billing. The upgrade buttons are disabled.

## Watermark

Free-tier clips get the logo in the top-right corner at 60% opacity, 18% of frame width.
Replace `webapp/assets/watermark.png` with your real logo, or point `CLIPFORGE_WATERMARK`
at another file. The current file is a placeholder badge.

## Before setting prices

Measure GPU time per minute of source on a real hour of footage, add the LLM cost, storage
and bandwidth, then check that every tier stays profitable at its full allowance.
