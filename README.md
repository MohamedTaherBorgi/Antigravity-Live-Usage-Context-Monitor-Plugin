# Antigravity Live Usage & Context Monitor and `usage-monitor` command

A lightweight, **zero-token** plugin for **Google Antigravity** that gives you a live side-panel dashboard and `/usage-stats` slash command to track your **Context Window (Memory)**, **Token Usage**, **Cache Savings Rate**, and **Estimated API Cost** in real time.

---

## ✨ Features

1. **🟢 100% Zero-Token Live Side Panel**
   - Reads Antigravity's local `transcript.jsonl` files directly from disk every 1.5 seconds.
   - **Never calls the AI** and **consumes 0 tokens**.
2. **🧠 1. Chat Memory (Context Window Meter)**
   - Shows exact percentage of the `1,000,000`-token window currently held in memory (`% Full`, `k / 1,000k tokens`, and approximate page count).
   - Automatically detects `/compact` resets.
   - Color-coded status bar (`Green < 40%`, `Yellow 40–75%`, `Red > 75%`) so you know when to run `/compact` or start a fresh chat.
3. **⚡ 2. Last Message Task Breakdown**
   - Displays the exact cost and token breakdown of your most recent message:
     - **Estimated Task Cost** (switchable between Gemini Pro and Gemini Flash pricing)
     - **AI Response Size** (output tokens + approximate word count)
     - **New Input Read** (uncached input tokens)
     - **Actions / Tool Steps** (how many internal model calls the turn took)
4. **📊 3. Whole Conversation Totals & History**
   - Cumulative cost, total output tokens, **Cache Savings Rate** (`% of chat history reused at 75% discount`), and a collapsible per-message cost log.
5. **⌨️ `/usage-stats` Slash Command**
   - Type `/usage-stats` in chat anytime to print a clean Markdown table of your context memory and turn-by-turn token usage.

<img width="722" height="972" alt="image" src="https://github.com/user-attachments/assets/9d90f6e6-2652-4c5c-a118-fa8238a8a473" />

---

## 🚀 Installation

Clone this repository directly into your Antigravity plugins directory:

### Windows (PowerShell)
```powershell
git clone https://github.com/<your-username>/antigravity-usage-monitor.git "$HOME\.gemini\config\plugins\usage-monitor"
```

### macOS / Linux
```bash
git clone https://github.com/<your-username>/antigravity-usage-monitor.git ~/.gemini/config/plugins/usage-monitor
```

### Enable the Plugin
1. Open Antigravity and go to **Customizations $\rightarrow$ Plugins**, then toggle **Antigravity Live Usage & Context Monitor** (`usage-monitor`) **ON**.
2. *(Optional)* Make sure `python` (Python 3) is installed and available in your system `PATH`.

---

## 📁 Repository Structure

```text
usage-monitor/
├── plugin.json
├── README.md
├── assets/
│   └── logo.svg
├── sidecars/
│   └── usage-monitor/
│       ├── sidecar.json
│       └── server.py
└── skills/
    └── usage-stats/
        ├── SKILL.md
        └── scripts/
            └── get_usage.ps1
```
