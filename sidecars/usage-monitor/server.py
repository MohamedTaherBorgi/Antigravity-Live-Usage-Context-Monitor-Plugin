import json
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BIND_HOST = "127.0.0.1"
PORT = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "18765"))
APP_DATA_DIR = Path(
    os.environ.get(
        "ANTIGRAVITY_APP_DATA_DIR",
        str(Path.home() / ".gemini" / "antigravity"),
    )
)
BRAIN_DIR = APP_DATA_DIR / "brain"
PRELOAD_PATH = APP_DATA_DIR / "lib" / "node_modules" / "sidecar_sdk" / "preload.js"

_PARSE_CACHE = {}
_MODELS_CACHE = {"ts": 0.0, "models": []}
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
EFFORT_SUFFIX_RE = re.compile(r"\s*\((?:Low|Medium|High|Thinking|Fast)\)\s*$", re.IGNORECASE)


def classify_tier(label: str, model_id: str = "") -> str:
    s = f"{label} {model_id}".lower()
    if "flash" in s or "lite" in s:
        return "flash"
    if "opus" in s:
        return "opus"
    if "sonnet" in s:
        return "sonnet"
    if "oss" in s or "llama" in s:
        return "oss"
    return "pro"


DEFAULT_MODELS = [
    {"id": "pro", "name": "Gemini Pro / Argon (Flagship)", "tier": "pro", "quotaPct": None, "resetTime": None},
    {"id": "flash", "name": "Gemini Flash (Fast Tier)", "tier": "flash", "quotaPct": None, "resetTime": None},
    {"id": "sonnet", "name": "Claude Sonnet", "tier": "sonnet", "quotaPct": None, "resetTime": None},
    {"id": "opus", "name": "Claude Opus", "tier": "opus", "quotaPct": None, "resetTime": None},
    {"id": "oss", "name": "GPT-OSS", "tier": "oss", "quotaPct": None, "resetTime": None},
]


def get_live_models():
    now = time.time()
    if _MODELS_CACHE["models"] and (now - _MODELS_CACHE["ts"] < 15.0):
        return _MODELS_CACHE["models"]

    ls_addr = (os.environ.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    csrf = (os.environ.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    if not ls_addr or not csrf:
        return DEFAULT_MODELS

    base_url = ls_addr if ls_addr.startswith("http") else f"http://{ls_addr}"
    url = f"{base_url.rstrip('/')}/exa.language_server_pb.LanguageServerService/GetUserStatus"

    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))

        configs = (
            payload.get("userStatus", {})
            .get("cascadeModelConfigData", {})
            .get("clientModelConfigs", [])
        )
        families = {}
        order = []
        for cfg in configs:
            raw_label = (cfg.get("label") or "").strip()
            if not raw_label:
                continue
            family_name = EFFORT_SUFFIX_RE.sub("", raw_label).strip() or raw_label
            model_id = cfg.get("modelId") or ""
            tier = classify_tier(family_name, model_id)
            q_info = cfg.get("quotaInfo") or {}
            rem = q_info.get("remainingFraction")
            quota_pct = round(float(rem) * 100, 1) if rem is not None else None
            reset_time = q_info.get("resetTime")

            if family_name not in families:
                families[family_name] = {
                    "id": family_name,
                    "name": family_name,
                    "tier": tier,
                    "quotaPct": quota_pct,
                    "resetTime": reset_time,
                }
                order.append(family_name)

        if order:
            # Sort so flagship Gemini Pro/Argon is first, then Flash, then others
            tier_rank = {"pro": 0, "flash": 1, "sonnet": 2, "opus": 3, "oss": 4}
            models_list = [families[k] for k in order]
            models_list.sort(key=lambda m: tier_rank.get(m["tier"], 9))
            _MODELS_CACHE["ts"] = now
            _MODELS_CACHE["models"] = models_list
            return models_list
    except Exception:
        pass

    return _MODELS_CACHE["models"] or DEFAULT_MODELS


def parse_transcript(transcript_path: Path):
    try:
        stat = transcript_path.stat()
    except OSError:
        return None

    cache_key = str(transcript_path)
    cached = _PARSE_CACHE.get(cache_key)
    if cached and cached["mtime_ns"] == stat.st_mtime_ns and cached["size"] == stat.st_size:
        return cached["data"]

    turns = []
    current_turn = None
    turn_idx = 0

    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    step = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                stype = step.get("type", "")
                if stype == "USER_INPUT":
                    if current_turn is not None:
                        turns.append(current_turn)
                    turn_idx += 1
                    content = step.get("content", "") or ""
                    m = USER_REQ_RE.search(content)
                    prompt_text = (m.group(1) if m else content).strip()
                    prompt_clean = " ".join(prompt_text.split())
                    current_turn = {
                        "turn": turn_idx,
                        "prompt": prompt_clean[:100] + ("..." if len(prompt_clean) > 100 else ""),
                        "steps": 0,
                        "inputTokens": 0,
                        "cacheReadTokens": 0,
                        "outputTokens": 0,
                        "contextSize": 0,
                    }
                elif stype == "PLANNER_RESPONSE" and current_turn is not None:
                    in_tok = int(step.get("input_tokens") or 0)
                    cache_tok = int(step.get("cache_read_tokens") or 0)
                    out_tok = int(step.get("output_tokens") or 0)

                    current_turn["steps"] += 1
                    current_turn["inputTokens"] += in_tok
                    current_turn["cacheReadTokens"] += cache_tok
                    current_turn["outputTokens"] += out_tok
                    current_turn["contextSize"] = in_tok + cache_tok + out_tok
                elif stype == "CHECKPOINT" and current_turn is not None:
                    summary_chars = len(step.get("content", "") or "")
                    est_ctx = 15500 + (summary_chars // 4)
                    current_turn["steps"] += 1
                    current_turn["contextSize"] = est_ctx
                    current_turn["prompt"] = "/compact (Context Reset)"
    except OSError:
        return None

    if current_turn is not None:
        turns.append(current_turn)

    completed_turns = [t for t in turns if t["steps"] > 0]
    last_turn = completed_turns[-1] if completed_turns else (turns[-1] if turns else None)

    total_in = sum(t["inputTokens"] for t in turns)
    total_cache = sum(t["cacheReadTokens"] for t in turns)
    total_out = sum(t["outputTokens"] for t in turns)
    total_steps = sum(t["steps"] for t in turns)
    active_ctx = last_turn["contextSize"] if last_turn else 0

    conv_id = transcript_path.parent.parent.name
    first_prompt = turns[0]["prompt"] if turns else conv_id[:8]

    data = {
        "conversationId": conv_id,
        "title": first_prompt,
        "updatedAtMs": int(stat.st_mtime * 1000),
        "turns": turns,
        "lastTurn": last_turn,
        "totals": {
            "turns": len(turns),
            "steps": total_steps,
            "inputTokens": total_in,
            "cacheReadTokens": total_cache,
            "outputTokens": total_out,
            "activeContext": active_ctx,
        },
    }

    _PARSE_CACHE[cache_key] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "data": data,
    }
    return data


def get_all_conversations():
    results = []
    if not BRAIN_DIR.exists():
        return results
    for conv_dir in BRAIN_DIR.iterdir():
        if not conv_dir.is_dir():
            continue
        t_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if t_path.exists():
            try:
                mtime = t_path.stat().st_mtime
                results.append((mtime, conv_dir.name, t_path))
            except OSError:
                pass
    results.sort(key=lambda x: x[0], reverse=True)
    return results


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Usage & Context</title>
<script src="/preload.js"></script>
<style>
  html, body {
    margin: 0 !important;
    padding: 0 !important;
    background: #0b1120 !important;
    color: #f8fafc !important;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif !important;
    font-size: 13px;
    line-height: 1.5;
  }
  .wrap {
    padding: 14px;
    max-width: 480px;
    margin: 0 auto;
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    gap: 8px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    font-size: 14px;
    color: #f8fafc;
    white-space: nowrap;
  }
  .dot-live {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #10b981;
    box-shadow: 0 0 8px #10b981;
  }
  .tier-select {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11.5px;
    cursor: pointer;
    max-width: 210px;
  }
  .card {
    background: #131c31;
    border: 1px solid #1e293b;
    border-radius: 12px;
    padding: 14px;
    margin-bottom: 12px;
  }
  .section-title {
    font-size: 11.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .section-desc {
    font-size: 12px;
    color: #cbd5e1;
    margin-bottom: 10px;
  }

  /* 1. Context Meter */
  .ctx-headline {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .ctx-pct {
    font-size: 24px;
    font-weight: 800;
    color: #34d399;
    font-variant-numeric: tabular-nums;
  }
  .ctx-tokens {
    font-size: 12px;
    color: #94a3b8;
    font-variant-numeric: tabular-nums;
  }
  .bar-bg {
    width: 100%;
    height: 10px;
    background: #090d16;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 8px;
    border: 1px solid #1e293b;
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #10b981, #38bdf8);
    border-radius: 999px;
    transition: width 0.3s ease;
  }
  .status-tip {
    font-size: 12px;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(16, 185, 129, 0.1);
    border: 1px solid rgba(16, 185, 129, 0.25);
    color: #a7f3d0;
  }

  /* Explained Metric Rows */
  .metric-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 0;
    border-bottom: 1px solid #1e293b;
  }
  .metric-row:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }
  .metric-left {
    display: flex;
    flex-direction: column;
    padding-right: 10px;
  }
  .metric-name {
    font-weight: 600;
    color: #f1f5f9;
    font-size: 13px;
  }
  .metric-help {
    font-size: 11px;
    color: #94a3b8;
  }
  .metric-right {
    text-align: right;
    font-weight: 700;
    font-size: 14px;
    color: #f8fafc;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .metric-right.price {
    color: #34d399;
    font-size: 15px;
  }
  .prompt-box {
    background: #090d16;
    border-left: 3px solid #38bdf8;
    padding: 7px 10px;
    border-radius: 4px;
    font-size: 12px;
    color: #e2e8f0;
    margin-bottom: 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .quota-mini-bar {
    width: 64px;
    height: 6px;
    background: #090d16;
    border-radius: 999px;
    overflow: hidden;
    display: inline-block;
    vertical-align: middle;
    margin-left: 6px;
    border: 1px solid #1e293b;
  }
  .quota-mini-fill {
    display: block;
    height: 100%;
    background: #10b981;
    border-radius: 999px;
  }

  details {
    background: #131c31;
    border: 1px solid #1e293b;
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 10px;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
    font-size: 12px;
    color: #94a3b8;
    outline: none;
  }
  .history-item {
    padding: 8px 0;
    border-bottom: 1px solid #1e293b;
    font-size: 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .history-item:last-child { border-bottom: none; }
  .history-prompt {
    color: #cbd5e1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .history-meta {
    color: #94a3b8;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="topbar">
    <div class="brand">
      <span class="dot-live"></span>
      <span>Chat Usage &amp; Memory</span>
    </div>
    <select id="tier-select" class="tier-select" title="Select active AI model to estimate API equivalent cost"></select>
  </div>

  <!-- 1. CONTEXT WINDOW (MEMORY) -->
  <div class="card">
    <div class="section-title">
      <span>1. Chat Memory (Context Window)</span>
      <span id="ctx-pages-equiv">~0 pages of text</span>
    </div>
    <div class="section-desc">
      How much of the AI's 1,000,000-token memory this conversation is currently holding:
    </div>

    <div class="ctx-headline">
      <span class="ctx-pct" id="ctx-pct">0.0% Full</span>
      <span class="ctx-tokens" id="ctx-tokens">0 / 1,000,000 tokens</span>
    </div>

    <div class="bar-bg">
      <div class="bar-fill" id="ctx-bar" style="width: 0%"></div>
    </div>

    <div class="status-tip" id="ctx-advice">
      🟢 Optimal memory zone (&lt;200k tokens). Fast responses &amp; standard 1x token rates.
    </div>
  </div>

  <!-- 2. LAST MESSAGE TASK -->
  <div class="card">
    <div class="section-title">
      <span>2. Last Message Task</span>
      <span id="last-turn-badge">Message #-</span>
    </div>
    <div class="section-desc">
      What your most recent message used:
    </div>

    <div class="prompt-box" id="last-prompt">Waiting for message...</div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">Estimated Task Cost</span>
        <span class="metric-help" id="last-cost-tier-label">API equivalent cost for this single turn</span>
      </div>
      <div class="metric-right price" id="last-cost">$0.000</div>
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">AI Response Size</span>
        <span class="metric-help">Tokens generated by the AI in this turn</span>
      </div>
      <div class="metric-right" id="last-out">0 tokens</div>
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">New Input Read</span>
        <span class="metric-help">New text/files processed (not yet in cache)</span>
      </div>
      <div class="metric-right" id="last-in">0 tokens</div>
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">Actions / Tool Steps</span>
        <span class="metric-help">How many internal steps the AI took</span>
      </div>
      <div class="metric-right" id="last-steps">1 step</div>
    </div>
  </div>

  <!-- 3. WHOLE CONVERSATION -->
  <div class="card">
    <div class="section-title">
      <span>3. Entire Conversation Total</span>
      <span id="total-turns">0 messages</span>
    </div>
    <div class="section-desc">
      Cumulative usage since the start of this chat:
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">Total Conversation Cost</span>
        <span class="metric-help">Sum of all messages in this chat</span>
      </div>
      <div class="metric-right price" id="total-cost">$0.00</div>
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">Total AI Output Written</span>
        <span class="metric-help">All explanations &amp; files written so far</span>
      </div>
      <div class="metric-right" id="total-out">0 tokens</div>
    </div>

    <div class="metric-row">
      <div class="metric-left">
        <span class="metric-name">Cache Savings Rate</span>
        <span class="metric-help">% of chat history reused at 75% discount</span>
      </div>
      <div class="metric-right" id="cache-rate" style="color: #38bdf8;">0%</div>
    </div>
  </div>

  <!-- 4. LIVE MODEL QUOTA REMAINING (Auto-detected from Antigravity) -->
  <div class="card" id="quota-card" style="display: none;">
    <div class="section-title">
      <span>4. Live Antigravity Model Quotas</span>
      <span id="quota-reset-badge" style="color: #38bdf8; font-weight: 600; text-transform: none;"></span>
    </div>
    <div class="section-desc">
      Remaining quota across your live Antigravity models:
    </div>
    <div id="quota-list"></div>
  </div>

  <!-- OPTIONAL: Past Messages List -->
  <details>
    <summary>📜 View Every Message in This Chat</summary>
    <div id="history-list" style="margin-top: 8px;"></div>
  </details>

  <!-- OPTIONAL: Quick Glossary -->
  <details>
    <summary>❓ What is a token &amp; when should I use /compact?</summary>
    <div style="margin-top: 8px; font-size: 12px; color: #cbd5e1;">
      <p style="margin: 0 0 6px 0;">• <strong>1,000 tokens</strong> ≈ 750 words (or ~1.5 pages of text).</p>
      <p style="margin: 0 0 6px 0;">• <strong>200k Token Threshold (20%):</strong> Standard 1x token rates apply up to 200k tokens. Above 200k tokens, long-context requests are billed/weighted at 2x.</p>
      <p style="margin: 0 0 6px 0;">• <strong>When to use <code>/compact</code>:</strong> Type <code>/compact</code> around 20%–50% to summarize older history and drop your context back down to ~2%.</p>
      <p style="margin: 0;">• <strong>Zero-Token Panel:</strong> This dashboard reads local log files on your PC—it uses 0 tokens itself.</p>
    </div>
  </details>

</div>

<script>
  const PRICING = {
    pro:    { label: 'Pro Tier ($1.25/$5)',    in: 1.25 / 1e6, cache: 0.3125 / 1e6, out: 5.00 / 1e6 },
    flash:  { label: 'Flash Tier ($0.15/$0.6)', in: 0.15 / 1e6, cache: 0.0375 / 1e6, out: 0.60 / 1e6 },
    sonnet: { label: 'Sonnet Tier ($3/$15)',   in: 3.00 / 1e6, cache: 0.3000 / 1e6, out: 15.0 / 1e6 },
    opus:   { label: 'Opus Tier ($5/$25)',     in: 5.00 / 1e6, cache: 0.5000 / 1e6, out: 25.0 / 1e6 },
    oss:    { label: 'OSS Tier ($0.5/$1.5)',   in: 0.50 / 1e6, cache: 0.1250 / 1e6, out: 1.50 / 1e6 }
  };

  const tierSelect = document.getElementById('tier-select');
  let lastData = null;
  let knownModelsSig = '';

  function fmtNum(n) {
    return (n || 0).toLocaleString();
  }

  function fmtK(n) {
    if (!n) return '0';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return n.toString();
  }

  function calcCost(inTok, cacheTok, outTok, tier) {
    const p = PRICING[tier] || PRICING.pro;
    return (inTok * p.in) + (cacheTok * p.cache) + (outTok * p.out);
  }

  function fmtCost(c) {
    if (c === 0) return '$0.000';
    if (c < 0.001) return '<$0.001';
    if (c < 0.10) return '$' + c.toFixed(3);
    return '$' + c.toFixed(2);
  }

  function fmtResetTime(isoStr) {
    if (!isoStr) return '';
    const diffMs = new Date(isoStr).getTime() - Date.now();
    if (isNaN(diffMs) || diffMs <= 0) return 'Resets soon';
    const totalMins = Math.floor(diffMs / 60000);
    const days = Math.floor(totalMins / 1440);
    const hrs = Math.floor((totalMins % 1440) / 60);
    const mins = totalMins % 60;
    if (days > 0) return `Resets in ${days}d ${hrs}h`;
    if (hrs > 0) return `Resets in ${hrs}h ${mins}m`;
    return `Resets in ${mins}m`;
  }

  function syncModelsDropdown(models) {
    if (!Array.isArray(models) || !models.length) return;
    const sig = models.map(m => `${m.id}:${m.tier}`).join('|');
    if (sig === knownModelsSig) return;
    const prevVal = tierSelect.value;
    knownModelsSig = sig;
    tierSelect.innerHTML = '';
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m.tier;
      opt.dataset.modelName = m.name;
      opt.textContent = m.name;
      tierSelect.appendChild(opt);
    }
    if (prevVal && [...tierSelect.options].some(o => o.value === prevVal)) {
      tierSelect.value = prevVal;
    }
  }

  function renderQuotas(models) {
    const card = document.getElementById('quota-card');
    const listEl = document.getElementById('quota-list');
    const resetBadge = document.getElementById('quota-reset-badge');
    const withQuota = (models || []).filter(m => typeof m.quotaPct === 'number');
    if (!withQuota.length) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';
    resetBadge.textContent = 'Live';

    listEl.innerHTML = '';
    for (const m of withQuota) {
      const pct = Math.max(0, Math.min(100, m.quotaPct));
      const color = pct > 50 ? '#34d399' : (pct > 20 ? '#fbbf24' : '#fb7185');
      const resetText = fmtResetTime(m.resetTime);
      const row = document.createElement('div');
      row.className = 'metric-row';
      row.innerHTML = `
        <div class="metric-left">
          <span class="metric-name">${m.name}</span>
          ${resetText ? `<span class="metric-help">${resetText}</span>` : ''}
        </div>
        <div class="metric-right" style="color:${color}">
          ${pct.toFixed(1)}% left
          <span class="quota-mini-bar">
            <span class="quota-mini-fill" style="width:${pct}%; background:${color}"></span>
          </span>
        </div>
      `;
      listEl.appendChild(row);
    }
  }

  function render(data) {
    if (!data) return;
    lastData = data;

    if (data.models) {
      syncModelsDropdown(data.models);
      renderQuotas(data.models);
    }

    if (!data.active) return;

    const active = data.active;
    const totals = active.totals || {};
    const lastTurn = active.lastTurn || {};
    const tier = tierSelect.value || 'pro';
    const selectedOpt = tierSelect.options[tierSelect.selectedIndex];
    const selectedName = selectedOpt ? selectedOpt.textContent : 'Selected Model';

    // 1. Context Window (out of 1,000,000)
    const ctxLimit = 1000000;
    const activeCtx = totals.activeContext || 0;
    const pct = Math.min(100, (activeCtx / ctxLimit) * 100);
    const approxPages = Math.max(1, Math.round(activeCtx / 650));

    document.getElementById('ctx-pct').textContent = pct.toFixed(1) + '% Full';
    document.getElementById('ctx-tokens').textContent = `${fmtK(activeCtx)} / 1,000k tokens`;
    document.getElementById('ctx-pages-equiv').textContent = `~${fmtNum(approxPages)} pages of text`;
    document.getElementById('ctx-bar').style.width = Math.max(1.5, pct) + '%';

    const adviceEl = document.getElementById('ctx-advice');
    const pctEl = document.getElementById('ctx-pct');
    if (pct < 20) {
      pctEl.style.color = '#34d399';
      adviceEl.style.background = 'rgba(16, 185, 129, 0.1)';
      adviceEl.style.borderColor = 'rgba(16, 185, 129, 0.25)';
      adviceEl.style.color = '#a7f3d0';
      adviceEl.textContent = '🟢 Optimal memory zone (<200k tokens). Fast responses & standard 1x token rates.';
    } else if (pct < 50) {
      pctEl.style.color = '#fbbf24';
      adviceEl.style.background = 'rgba(251, 191, 36, 0.12)';
      adviceEl.style.borderColor = 'rgba(251, 191, 36, 0.3)';
      adviceEl.style.color = '#fde68a';
      adviceEl.textContent = '🟡 Passed 200k tokens (2x long-context tier). Type /compact anytime to compress history.';
    } else {
      pctEl.style.color = '#fb7185';
      adviceEl.style.background = 'rgba(251, 113, 133, 0.12)';
      adviceEl.style.borderColor = 'rgba(251, 113, 133, 0.3)';
      adviceEl.style.color = '#fecdd3';
      adviceEl.textContent = '🔴 Context memory is heavy! Run /compact now or start a new chat for a fresh task.';
    }

    // 2. Last Message Task
    document.getElementById('last-turn-badge').textContent = lastTurn.turn ? `Message #${lastTurn.turn}` : 'Message #-';
    document.getElementById('last-prompt').textContent = lastTurn.prompt ? `"${lastTurn.prompt}"` : 'Waiting for message...';
    document.getElementById('last-cost-tier-label').textContent = `API equivalent cost (${selectedName})`;
    const lastCost = calcCost(lastTurn.inputTokens || 0, lastTurn.cacheReadTokens || 0, lastTurn.outputTokens || 0, tier);
    document.getElementById('last-cost').textContent = fmtCost(lastCost);

    const lastWords = Math.round((lastTurn.outputTokens || 0) * 0.75);
    document.getElementById('last-out').textContent = `${fmtK(lastTurn.outputTokens || 0)} tokens (~${fmtNum(lastWords)} words)`;
    document.getElementById('last-in').textContent = `${fmtK(lastTurn.inputTokens || 0)} tokens`;
    document.getElementById('last-steps').textContent = `${lastTurn.steps || 0} step${lastTurn.steps === 1 ? '' : 's'}`;

    // 3. Whole Conversation
    document.getElementById('total-turns').textContent = `${totals.turns || 0} messages`;
    const totalCost = calcCost(totals.inputTokens || 0, totals.cacheReadTokens || 0, totals.outputTokens || 0, tier);
    document.getElementById('total-cost').textContent = fmtCost(totalCost);
    document.getElementById('total-out').textContent = `${fmtK(totals.outputTokens || 0)} tokens`;

    const allIn = (totals.inputTokens || 0) + (totals.cacheReadTokens || 0);
    const cacheRate = allIn > 0 ? ((totals.cacheReadTokens / allIn) * 100).toFixed(0) : '0';
    document.getElementById('cache-rate').textContent = `${cacheRate}% reused from cache`;

    // 4. History dropdown
    const histEl = document.getElementById('history-list');
    histEl.innerHTML = '';
    const turnsRev = [...(active.turns || [])].reverse();
    for (const t of turnsRev) {
      const c = calcCost(t.inputTokens, t.cacheReadTokens, t.outputTokens, tier);
      const row = document.createElement('div');
      row.className = 'history-item';
      row.innerHTML = `
        <div class="history-prompt" title="${(t.prompt || '').replace(/"/g, '&quot;')}">
          <strong>#${t.turn}</strong> ${t.prompt || ''}
        </div>
        <div class="history-meta">
          ${fmtK(t.outputTokens)} out • <strong style="color:#34d399">${fmtCost(c)}</strong>
        </div>
      `;
      histEl.appendChild(row);
    }
  }

  async function poll() {
    try {
      const fetcher = (window.sidecar && window.sidecar.fetch) ? window.sidecar.fetch : fetch;
      const res = await fetcher('/api/stats');
      if (res.ok) {
        const data = await res.json();
        render(data);
      }
    } catch (e) {}
  }

  tierSelect.addEventListener('change', () => render(lastData));
  poll();
  setInterval(poll, 1500);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/preload.js":
            if PRELOAD_PATH.exists():
                self._send(
                    200,
                    "application/javascript; charset=utf-8",
                    PRELOAD_PATH.read_bytes(),
                )
            else:
                self._send(200, "application/javascript; charset=utf-8", b"")
            return

        if parsed.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML_PAGE.encode("utf-8"))
            return

        if parsed.path == "/api/stats":
            qs = parse_qs(parsed.query)
            req_conv_id = (qs.get("conversationId", [""])[0] or "").strip()
            all_convs = get_all_conversations()

            conv_list = []
            selected_data = None

            for _, cid, t_path in all_convs[:20]:
                parsed_conv = parse_transcript(t_path)
                if not parsed_conv:
                    continue
                conv_list.append(
                    {
                        "id": cid,
                        "title": parsed_conv["title"],
                        "updatedAtMs": parsed_conv["updatedAtMs"],
                    }
                )
                if req_conv_id and cid == req_conv_id:
                    selected_data = parsed_conv
                elif not req_conv_id and selected_data is None:
                    selected_data = parsed_conv

            if selected_data is None and conv_list:
                first_path = BRAIN_DIR / conv_list[0]["id"] / ".system_generated" / "logs" / "transcript.jsonl"
                selected_data = parse_transcript(first_path)

            payload = {
                "conversations": conv_list,
                "active": selected_data,
                "models": get_live_models(),
            }
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(payload).encode("utf-8"),
            )
            return

        self._send(404, "text/plain; charset=utf-8", b"Not Found")


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"[usage-monitor] listening on http://{BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
