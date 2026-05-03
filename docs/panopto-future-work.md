# Panopto transcription — future work

## Why skipped
ND ACMS 30440 instructor disabled captions on all videos. `DeliveryInfo.aspx`
returns `HasCaptions: false, AvailableCaptions: []`. No caption track exists
to scrape, regardless of LTI launch / browser auth.

## Path to fix
1. **Capture HLS stream URL** via Playwright after LTI launch — Panopto loads
   `*.m3u8` playlists in network log.
2. **Download audio** via `ffmpeg -i <m3u8> -vn -acodec copy out.m4a`.
3. **Transcribe with Gemini Files API** — upload audio, ask for verbatim text
   with timestamps. Cost ~$0.50 per 30-min lecture on Gemini 2.5 Pro.
4. **Or local Whisper** — `faster-whisper large-v3-turbo`, ~real-time on
   M-series Metal, free.

## Alternative: instructor-side fix
Could ask instructor to flip "Generate captions" toggle on Panopto course
folder — Panopto auto-transcribes via own ASR within ~24h. Free.

## Skip marker
`downloads/<course>/.panopto_skip` exists → `download.py` skips Panopto
attempt on future syncs. Delete to retry.
