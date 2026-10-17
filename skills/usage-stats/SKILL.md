---
name: usage-stats
description: >-
  Display token usage (uncached input, cached reads, output tokens, active context window size)
  and estimated API cost for the last message task and the entire conversation.
  Trigger whenever the user says "usage stats", "/usage-stats", "tokens", "cost", or asks how many tokens a task or conversation used.
---

# Conversation & Task Usage Stats (`usage-stats`)

When the user asks for usage stats, token counts, or conversation cost, run the bundled PowerShell script against the current conversation's `transcript.jsonl` and present a clean Markdown summary.

## Steps

1. Run the helper script on the current conversation's transcript (using `$HOME` so it works on any user's machine):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\.gemini\config\plugins\usage-monitor\skills\usage-stats\scripts\get_usage.ps1" -TranscriptPath "<appDataDir>\brain\<conversation-id>\.system_generated\logs\transcript.jsonl"
   ```
2. Format the output into three clear sections:
   - **1. Chat Memory (Active Context Window):** Show `ActiveContextTokens / 1,000,000` (`% Full` and approximate pages of text), and whether memory is healthy (`<20%`), moderate (`20%–50%`), or ready for `/compact`.
   - **2. Last Completed Message Task:** Steps count, New Input Read (uncached), Cached History Reused, AI Output tokens, and Estimated Cost.
   - **3. Recent Turns & Full Conversation Totals:** Table of recent turns and cumulative totals across the conversation.
   - Estimate cost using standard Gemini Pro API rates (`$1.25 / 1M` uncached input, `$0.3125 / 1M` cached input, `$5.00 / 1M` output) and Gemini Flash rates (`$0.15 / 1M` uncached input, `$0.0375 / 1M` cached input, `$0.60 / 1M` output).
